from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from codex_watchtower.cli import build_parser
from codex_watchtower.models.codex_cli import build_command
from codex_watchtower.report.contract import ProgressReport, validate_evidence
from codex_watchtower.report.engine import PACKET_BUDGET, ReportEngine
from codex_watchtower.report.service import select_source
from codex_watchtower.report.web import create_report_app, export_html, render_html, report_events
from codex_watchtower.storage.db import open_database
from codex_watchtower.storage.repository import Repository

FIXTURE = Path(__file__).parents[1] / "fixtures/progress/rollout-real-envelope.jsonl"


def report_for(ref: str) -> ProgressReport:
    claim = {"text": "Тесты прошли; публикация не проверена.", "refs": [ref]}
    return ProgressReport.model_validate(
        {
            "schema_version": "1.0",
            "goal": "Подготовить отчёт",
            "scope_changed": False,
            "summary": claim,
            "done": [claim],
            "remaining": [claim],
            "current_action": claim,
            "blockers": [],
            "progress": {"low": 40, "high": 60},
            "progress_reason": "Реализация готовится, приёмка впереди.",
            "eta": None,
            "uncertainty": "Публикация не подтверждена.",
            "tasks": [
                {
                    "id": "report",
                    "title": "Отчёт",
                    "status": "in_progress",
                    "origin": "user",
                    "active": True,
                    "comment": "Проверяется.",
                    "refs": [ref],
                }
            ],
        }
    )


@pytest.fixture
def engine(tmp_path):
    source = tmp_path / "rollout.jsonl"
    shutil.copyfile(FIXTURE, source)
    conn = open_database(tmp_path / "state.sqlite")
    observer = ReportEngine(Repository(conn), source, tmp_path / "report.html")
    yield observer
    conn.close()


def append(engine, text="Работа продолжается", terminated=True):
    record = {
        "timestamp": "2026-10-22T09:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }
    with engine.source.open("a") as stream:
        stream.write(json.dumps(record) + ("\n" if terminated else ""))


@pytest.mark.asyncio
async def test_goal_completion_is_visible_ahead_of_historical_assessment(engine):
    engine.ingest()
    _, _, previous = engine.packet()
    assert previous is None
    ref = engine.repo.get_events_since(engine.session_id, None)[0]["logical_event_id"]
    engine.assessor = AsyncMock(return_value=report_for(ref))
    await engine.assess()
    assert engine.snapshot()["report_is_current"]

    def goal_output(status, thread_id):
        return {
            "type": "response_item",
            "timestamp": "2026-10-22T09:05:00Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "goal-call",
                "output": json.dumps(
                    [
                        {"text": "x" * 8000},
                        {"text": json.dumps({"goal": {"threadId": thread_id, "status": status}})},
                    ]
                ),
            },
        }

    with engine.source.open("a") as stream:
        stream.write(json.dumps(goal_output("complete", engine.session_id)) + "\n")
    engine.ingest()
    assert engine.snapshot()["reported_goal"] is None  # arbitrary tool text is not a goal result
    with engine.source.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "goal-call",
                        "input": "text(await tools.update_goal({status: 'complete'}));",
                    },
                }
            )
            + "\n"
        )
        stream.write(json.dumps(goal_output("complete", "other-session")) + "\n")
        stream.write(json.dumps(goal_output("complete", engine.session_id)) + "\n")
    engine.ingest()
    snapshot = engine.snapshot()
    assert snapshot["reported_goal"]["status"] == "complete"
    assert snapshot["reported_goal"]["at"] == "2026-10-22T09:05:00Z"
    assert not snapshot["report_is_current"]
    assert snapshot["report"]["tasks"][0]["status"] == "in_progress"
    assert engine.assessor.await_count == 1  # current goal status needs no model call
    restored = ReportEngine(engine.repo, engine.source, engine.output)
    assert restored.snapshot()["reported_goal"] == snapshot["reported_goal"]
    assert not snapshot["work_after_goal"]
    with engine.source.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-10-22T09:05:01Z",
                    "payload": {"type": "task_complete"},
                }
            )
            + "\n"
        )
    engine.ingest()
    assert not engine.snapshot()["work_after_goal"]  # closing the original turn is not new work
    with engine.source.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-10-22T09:06:00Z",
                    "payload": {"type": "task_started", "turn_id": "new-work"},
                }
            )
            + "\n"
        )
    engine.ingest()
    resumed = engine.snapshot()
    assert resumed["reported_goal"]["status"] == "complete"
    assert resumed["work_after_goal"] is True
    assert resumed["last_turn_state"] == "active_turn"
    assert resumed["last_turn_at"] == "2026-10-22T09:06:00Z"
    assert engine.assessor.await_count == 1
    with engine.source.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-10-22T09:07:00Z",
                    "payload": {"type": "task_complete", "turn_id": "new-work"},
                }
            )
            + "\n"
        )
    engine.ingest()
    assert engine.snapshot()["work_after_goal"] is True
    assert engine.snapshot()["last_turn_state"] == "between_turns"
    with engine.source.open("a") as stream:
        stream.write(json.dumps(goal_output("active", engine.session_id)) + "\n")
    engine.ingest()
    assert engine.snapshot()["reported_goal"]["status"] == "active"
    assert not engine.snapshot()["work_after_goal"]
    with engine.source.open("a") as stream:
        stream.write(json.dumps(goal_output("complete", engine.session_id)) + "\n")
    engine.ingest()
    assert not engine.snapshot()["work_after_goal"]  # a newer completion replaces the previous goal


def test_goal_status_does_not_come_from_prose_or_another_thread():
    from codex_watchtower.codex.goal import reported_goal

    assert reported_goal("Goal achieved", "session") is None
    assert reported_goal({"goal": {"threadId": "other", "status": "complete"}}, "session") is None
    assert reported_goal({"goal": {"threadId": "session", "status": []}}, "session") is None
    assert reported_goal({"goal": {"threadId": "session", "status": "invalid"}}, "session") is None
    assert reported_goal('{"goal":', "session") is None
    assert reported_goal(
        {"output": {"goal": {"threadId": "session", "status": "blocked"}}}, "session"
    ) == {"status": "blocked"}


def test_real_envelopes_partial_restart_and_replacement(engine):
    engine.ingest()
    rows = engine.repo.get_events_since(engine.session_id, None)
    assert any(r["source_type"] == "user" and "Добавь" in r["summary"] for r in rows)
    assert any("update_plan" in r["summary"] for r in rows)
    assert any(r["exit_code"] == 0 and "3 passed" in r["summary"] for r in rows)
    assert any("Compaction boundary" in r["summary"] for r in rows)
    assert not any("encrypted_content" in r["summary"] for r in rows)
    count = len(rows)
    append(engine, terminated=False)
    engine.ingest()
    assert len(engine.repo.get_events_since(engine.session_id, None)) == count
    with engine.source.open("a") as stream:
        stream.write("\n")
    restarted = ReportEngine(engine.repo, engine.source, engine.output)
    restarted.ingest()
    assert len(engine.repo.get_events_since(engine.session_id, None)) == count + 1
    original = engine.source.read_bytes()
    replacement = engine.source.with_suffix(".new")
    replacement.write_bytes(original)
    replacement.replace(engine.source)
    restarted.ingest()
    assert len(engine.repo.get_events_since(engine.session_id, None)) == count + 1
    engine.source.write_text('{"type":"other"}\n')
    with pytest.raises(ValueError, match="checkpoint"):
        restarted.ingest()


@pytest.mark.asyncio
async def test_analysis_evidence_and_cursors_survive_restart(engine):
    engine.ingest()
    packet, cursor, _ = engine.packet()
    ref = json.loads(packet)["events"][-1]["id"]
    engine.assessor = AsyncMock(return_value=report_for(ref))
    await engine.assess()
    assert engine.repo.latest_progress(engine.session_id)["event_cursor"] == cursor
    assert engine.output.exists()
    assert not engine.due(time.time())
    append(engine)
    engine.ingest()
    engine.bootstrap_target = -1
    assert not engine.due(time.time())
    assert not engine.due(time.time() + 601)
    assert engine.live_due(time.time() + 601)
    packet, _, previous = engine.packet()
    assert previous.tasks[0].id == "report"
    assert any(e["id"] == ref for e in json.loads(packet)["previous_evidence"])
    reopened = ReportEngine(engine.repo, engine.source, engine.output)
    assert reopened.snapshot()["report"]["tasks"][0]["id"] == "report"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ValueError("invalid JSON"), TimeoutError(), RuntimeError("quota")]
)
async def test_failure_keeps_previous_and_retries_later(engine, failure):
    engine.ingest()
    ref = json.loads(engine.packet()[0])["events"][0]["id"]
    engine.assessor = AsyncMock(return_value=report_for(ref))
    await engine.assess(live=True)
    version = engine.snapshot()["version"]
    append(engine)
    engine.ingest()
    engine.assessor = AsyncMock(side_effect=failure)
    await engine.assess(live=True)
    assert engine.snapshot()["version"] == version
    assert engine.snapshot()["error"]
    if isinstance(failure, RuntimeError):
        assert "вход" not in engine.snapshot()["error"]
    assert not engine.live_due(time.time())
    assert engine.live_due(time.time() + 601)


@pytest.mark.asyncio
async def test_cancelled_analysis_is_not_a_cli_failure(engine):
    engine.ingest()
    started = asyncio.Event()

    async def assess(packet, model):
        started.set()
        await asyncio.Future()

    engine.assessor = assess
    task = asyncio.create_task(engine.assess())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.snapshot()["error"] is None
    assert engine.snapshot()["analyzing"] is False
    assert engine.due(time.time())


@pytest.mark.asyncio
async def test_single_analysis_and_new_events_during_call(engine):
    engine.ingest()
    started, finish = asyncio.Event(), asyncio.Event()

    async def assess(packet, model):
        started.set()
        await finish.wait()
        return report_for(json.loads(packet)["events"][0]["id"])

    engine.assessor = assess
    task = asyncio.create_task(engine.assess())
    await started.wait()
    assert not engine.due(time.time() + 10000)
    await engine.assess()  # Coalesced while the original call is in flight.
    append(engine)
    engine.ingest()
    finish.set()
    await task
    snapshot = engine.snapshot()
    assert snapshot["coverage"]["analyzed_sequence"] < snapshot["coverage"]["latest_sequence"]


def test_packet_budget_and_unknown_malformed_records(engine):
    for _ in range(40):
        append(engine, "x" * 9000)
    with engine.source.open("a") as stream:
        stream.write('not-json\n{"type":"future","payload":{}}\n')
    engine.ingest()
    packet, cursor, _ = engine.packet()
    assert len(packet) <= PACKET_BUDGET
    assert cursor < engine.repo.max_event_sequence(engine.session_id)
    assert engine.snapshot()["coverage"]["malformed_count"] == 1
    assert engine.snapshot()["coverage"]["truncated_count"] == 40


def test_reference_validation_task_identity_and_scope():
    previous = report_for("e1")
    with pytest.raises(ValueError, match="outside"):
        validate_evidence(previous, {"wrong"}, None)
    new = previous.model_copy(deep=True)
    new.tasks = []
    with pytest.raises(ValueError, match="retained"):
        validate_evidence(new, {"e1"}, previous)
    for status in ["done", "in_progress", "unknown"]:
        new = previous.model_copy(deep=True)
        new.tasks[0].status = status
        new.tasks[0].active = False
        new.scope_changed = True
        new.progress = None
        new.eta = None
        validate_evidence(new, {"e1"}, previous)
    data = previous.model_dump()
    data["progress"] = {"low": 90, "high": 20}
    with pytest.raises(ValidationError):
        ProgressReport.model_validate(data)


@pytest.mark.asyncio
async def test_invalid_reference_never_published(engine):
    engine.ingest()
    engine.assessor = AsyncMock(return_value=report_for("invented"))
    await engine.assess()
    assert engine.snapshot()["version"] == 0
    assert engine.snapshot()["error"]


def test_html_embedding_atomic_export_and_failed_write(engine, monkeypatch):
    snapshot = engine.snapshot()
    snapshot["session_id"] = '</script><script>alert("x")</script>'
    html = render_html(snapshot)
    assert "</script><script>alert" not in html
    assert "\\u003c/script>" in html
    export_html(engine.output, snapshot)
    before = engine.output.read_bytes()
    monkeypatch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    engine.export()
    assert engine.output.read_bytes() == before
    assert engine.export_error
    assert not list(engine.output.parent.glob(".watchtower-*"))


@pytest.mark.asyncio
async def test_http_only_exposes_report_and_reconnect_sends_snapshot(engine):
    engine.ingest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_report_app(engine)), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.get("/")).status_code == 200
        response = await client.get("/report.json")
        assert response.json()["session_id"] == engine.session_id
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert (await client.get("/../../rollout.jsonl")).status_code == 404
        assert (await client.get("/", headers={"Host": "evil.example"})).status_code == 400
    request = AsyncMock()
    request.is_disconnected.return_value = False
    for _ in range(2):
        stream = report_events(engine, request)
        event = await anext(stream)
        assert event.startswith("id: ") and "\ndata: " in event
        assert json.loads(event.split("data: ")[1])["session_id"] == engine.session_id
        await stream.aclose()


def test_cli_validation_selection_and_schema(tmp_path, monkeypatch):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["report", "--session", "x", "--log", "x"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(ValueError, match="stdin"):
        select_source(parser.parse_args(["report"]), tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        select_source(parser.parse_args(["report", "--session", "absent"]), tmp_path)
    schema = Path(__file__).parents[2] / "schemas/progress_report.schema.json"
    assert json.loads(schema.read_text()) == ProgressReport.model_json_schema()
    command = build_command(tmp_path, "gpt-6-luna")
    assert "--ignore-user-config" in command and "--ephemeral" in command
    assert "shell_tool" in command and 'web_search="disabled"' in command
    assert "--sandbox" in command and "read-only" in command


@pytest.mark.asyncio
async def test_model_output_redaction_does_not_break_json(engine):
    engine.ingest()
    ref = json.loads(engine.packet()[0])["events"][0]["id"]
    report = report_for(ref)
    report.goal = "password=abcdefghij"
    engine.assessor = AsyncMock(return_value=report)
    await engine.assess()
    assert "abcdefghij" not in engine.repo.latest_progress(engine.session_id)["body"]
    assert "REDACTED" in engine.snapshot()["report"]["goal"]


def test_source_cannot_be_overwritten_by_output(engine):
    with pytest.raises(ValueError):
        ReportEngine(
            engine.repo, engine.source, engine.source.parent / "x" / ".." / engine.source.name
        )


def test_output_schema_constrains_model_to_real_evidence():
    from codex_watchtower.models.codex_cli import output_schema

    packet = json.dumps({"events": [{"id": "real"}], "previous_evidence": [{"id": "old"}]})
    schema = output_schema(packet)
    for name in ["Claim", "Task"]:
        assert schema["$defs"][name]["properties"]["refs"]["items"]["enum"] == ["old", "real"]


@pytest.mark.asyncio
async def test_restart_preserves_interval_even_with_new_events(engine):
    engine.ingest()
    ref = json.loads(engine.packet()[0])["events"][0]["id"]
    engine.assessor = AsyncMock(return_value=report_for(ref))
    await engine.assess(live=True)
    append(engine)
    restarted = ReportEngine(engine.repo, engine.source, engine.output)
    restarted.ingest()
    assert not restarted.live_due(time.time())
    assert restarted.live_due(time.time() + 601)


def test_turn_completion_is_separate_from_process_completion(engine):
    engine.ingest()
    assert engine.snapshot()["last_turn_state"] == "active_turn"  # Resumed second turn.
    with engine.source.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-10-22T09:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                }
            )
            + "\n"
        )
    engine.ingest()
    assert engine.snapshot()["last_turn_state"] == "between_turns"
    assert engine.snapshot()["process_state"] == "unverified"


def test_initial_history_is_read_in_batches_without_losing_catchup_boundary(tmp_path):
    source = tmp_path / "rollout.jsonl"
    content = FIXTURE.read_text()
    source.write_text(content + (content.splitlines()[-1] + "\n") * 4500)
    conn = open_database(tmp_path / "state.sqlite")
    try:
        engine = ReportEngine(Repository(conn), source, tmp_path / "report.html")
        engine.ingest()
        assert len(engine.repo.get_events_since(engine.session_id, None)) < 2020
        assert not engine.snapshot()["coverage"]["initial_history_fully_read"]
        original_end = engine.repo.get_progress_source(engine.session_id)["bootstrap_bytes"]
        reopened = ReportEngine(engine.repo, engine.source, engine.output)
        reopened.ingest()
        assert (
            reopened.repo.get_progress_source(engine.session_id)["bootstrap_bytes"] == original_end
        )
        reopened.ingest()
        assert reopened.snapshot()["coverage"]["initial_history_fully_read"]
        assert len(reopened.repo.get_events_since(engine.session_id, None)) == 4512
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_live_window_prioritizes_tail_without_advancing_full_history(engine):
    engine.ingest()
    rows = engine.repo.progress_events(engine.session_id, -1)
    engine.repo.save_progress(
        engine.session_id,
        rows[0]["event_sequence"],
        "2026-10-22T08:00:00Z",
        report_for(rows[0]["logical_event_id"]).model_dump_json(),
    )
    engine.assessor = AsyncMock(
        side_effect=lambda packet, model: report_for(json.loads(packet)["events"][-1]["id"])
    )
    assert engine.live_due(time.time())
    packet, cursor, _ = engine.packet(live=True)
    data = json.loads(packet)
    assert data["coverage"]["mode"] == "recent"
    assert data["coverage"]["window_minutes"] == 5
    assert cursor == rows[-1]["event_sequence"]
    assert data["events"][0]["timestamp"] >= data["coverage"]["window_start"]
    await engine.assess(live=True)
    snapshot = engine.snapshot()
    assert snapshot["report_mode"] == "recent"
    assert snapshot["report_is_current"]
    assert snapshot["coverage"]["analyzed_sequence"] == rows[0]["event_sequence"]
    assert not engine.live_due(time.time())
    restored = ReportEngine(engine.repo, engine.source, engine.output)
    assert restored.snapshot()["report_mode"] == "recent"
    assert (
        restored.repo.latest_progress(engine.session_id)["event_cursor"]
        == rows[0]["event_sequence"]
    )
    append(engine)
    engine.ingest()
    assert engine.snapshot()["report_is_current"]  # continuous tools do not hide a fresh report
    assert engine.live_due(time.time() + 301)


@pytest.mark.asyncio
async def test_live_timeout_preserves_report_and_full_history_cursor(engine, monkeypatch):
    import codex_watchtower.report.engine as module

    engine.ingest()
    ref = engine.repo.progress_events(engine.session_id, -1)[-1]["logical_event_id"]
    engine.assessor = AsyncMock(return_value=report_for(ref))
    await engine.assess(live=True)
    version = engine.snapshot()["version"]
    monkeypatch.setattr(module, "LIVE_TIMEOUT", 0.02)

    async def hung(packet, model):
        await asyncio.sleep(60)

    engine.assessor = hung
    await asyncio.wait_for(engine.assess(live=True), 0.3)
    assert engine.snapshot()["version"] == version
    assert "время" in engine.snapshot()["error"]
    assert engine.repo.latest_progress(engine.session_id) is None


def test_recent_packet_keeps_newest_events_and_reports_omissions(engine):
    engine.ingest()
    for i in range(100):
        append(engine, str(i) + "x" * 4000)
    engine.ingest()
    packet, cursor, _ = engine.packet(live=True)
    data = json.loads(packet)
    assert len(packet) <= PACKET_BUDGET
    assert data["coverage"]["omitted_events"] > 0
    assert cursor == engine.repo.max_event_sequence(engine.session_id)
    assert data["events"][-1]["summary"].startswith("99x")
    assert all(e["timestamp"] >= data["coverage"]["window_start"] for e in data["events"])


@pytest.mark.asyncio
async def test_full_history_runs_once_then_only_recent_window(engine):
    engine.ingest()
    ref = engine.repo.progress_events(engine.session_id, -1)[-1]["logical_event_id"]
    engine.assessor = AsyncMock(return_value=report_for(ref))
    await engine.assess()
    assert engine.snapshot()["coverage"]["history_complete"]
    append(engine)
    engine.ingest()
    assert not engine.due(time.time() + 10000)
    assert engine.live_due(time.time() + 10000)


def test_partial_initial_line_does_not_spin_ingestion(engine):
    append(engine, terminated=False)
    engine.repo.connection.execute(
        "UPDATE progress_sources SET bootstrap_bytes=? WHERE session_id=?",
        (engine.source.stat().st_size, engine.session_id),
    )
    assert engine.ingest()
    assert engine.bootstrap_target is None
    assert not engine.due(time.time())
    assert not engine.ingest()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["missing_tasks", "unknown_refs", "report_too_large", "schema"])
async def test_invalid_model_report_is_corrected_without_waiting_five_minutes(engine, reason):
    engine.ingest()
    ref = engine.repo.progress_events(engine.session_id, -1)[-1]["logical_event_id"]
    valid = report_for(ref)
    engine.assessor = AsyncMock(return_value=valid)
    await engine.assess(live=True)
    append(engine)
    engine.ingest()
    invalid: ProgressReport | Exception
    if reason == "missing_tasks":
        invalid = valid.model_copy(update={"tasks": []})
    elif reason == "unknown_refs":
        invalid = valid.model_copy(
            update={
                "summary": valid.summary.model_copy(update={"refs": ["invented"]}),
            }
        )
    elif reason == "report_too_large":
        invalid = valid.model_copy(
            update={
                "tasks": [valid.tasks[0]]
                + [
                    valid.tasks[0].model_copy(
                        update={
                            "id": f"extra_{i}",
                            "title": "x" * 160,
                            "comment": "x" * 600,
                        }
                    )
                    for i in range(39)
                ]
            }
        )
    else:
        try:
            ProgressReport.model_validate({})
        except ValidationError as exc:
            invalid = exc
    engine.assessor = AsyncMock(side_effect=[invalid, valid])
    await engine.assess(live=True)
    assert engine.assessor.await_count == 2
    retry_packet = json.loads(engine.assessor.await_args_list[1].args[0])
    assert retry_packet["validation_feedback"]["reason"] == reason
    assert len(engine.assessor.await_args_list[1].args[0]) <= PACKET_BUDGET
    assert engine.snapshot()["error"] is None
    assert engine.snapshot()["report_is_current"]


@pytest.mark.asyncio
async def test_repeated_invalid_report_has_specific_safe_error(engine):
    engine.ingest()
    ref = engine.repo.progress_events(engine.session_id, -1)[-1]["logical_event_id"]
    valid = report_for(ref)
    engine.assessor = AsyncMock(return_value=valid)
    await engine.assess(live=True)
    version = engine.snapshot()["version"]
    append(engine)
    engine.ingest()
    engine.assessor = AsyncMock(return_value=valid.model_copy(update={"tasks": []}))
    await engine.assess(live=True)
    assert engine.assessor.await_count == 2
    assert engine.snapshot()["version"] == version
    assert "задач" in engine.snapshot()["error"]
    assert "бюджет" not in engine.snapshot()["error"]


@pytest.mark.asyncio
async def test_correction_shares_the_original_deadline(engine, monkeypatch):
    import codex_watchtower.report.engine as module

    engine.ingest()
    ref = engine.repo.progress_events(engine.session_id, -1)[-1]["logical_event_id"]
    valid = report_for(ref)
    engine.assessor = AsyncMock(return_value=valid)
    await engine.assess(live=True)
    append(engine)
    engine.ingest()
    monkeypatch.setattr(module, "LIVE_TIMEOUT", 0.05)
    calls = 0

    async def slow_invalid(packet, model):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.035)
        return valid.model_copy(update={"tasks": []})

    engine.assessor = slow_invalid
    start = time.monotonic()
    await engine.assess(live=True)
    assert calls == 2
    assert time.monotonic() - start < 0.15
    assert "время" in engine.snapshot()["error"]


def test_recent_scope_uses_real_requests_not_partial_archive_or_internal_context(engine):
    engine.ingest()
    rows = engine.repo.progress_events(engine.session_id, -1)
    engine.repo.save_progress(
        engine.session_id,
        rows[0]["event_sequence"],
        "2026-10-22T08:00:00Z",
        report_for(rows[0]["logical_event_id"]).model_dump_json(),
    )
    for ts, text in [
        ("2026-10-22T08:01:03Z", "Больше не используй GitHub Actions"),
        (
            "2026-10-22T08:01:04Z",
            '<codex_internal_context source="goal">Старая цель</codex_internal_context>',
        ),
    ]:
        with engine.source.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "timestamp": ts,
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"text": text}]},
                    }
                )
                + "\n"
            )
    engine.ingest()
    packet, _, previous = engine.packet(live=True)
    data = json.loads(packet)
    assert previous is None
    assert data["previous_report"] is None  # archival guesses must not become the live task
    assert (
        data["request_context"]["latest_request"]["summary"] == "Больше не используй GitHub Actions"
    )
    assert all(
        "codex_internal_context" not in r["summary"]
        for r in data["request_context"]["user_messages"]
    )
    assert data["coverage"]["scope_request_id"]


def test_recent_packet_keeps_messages_and_completion_before_verbose_tool_outputs(engine):
    engine.ingest()
    for kind, payload in [
        (
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "text": (
                            "Исправлено и отправлено; 602 теста прошли. "
                            "Python повторно не запускал."
                        )
                    }
                ],
            },
        ),
        (
            "event_msg",
            {
                "type": "task_complete",
                "last_agent_message": (
                    "Исправлено и отправлено; 602 теста прошли. Python повторно не запускал."
                ),
            },
        ),
    ]:
        with engine.source.open("a") as stream:
            stream.write(
                json.dumps({"timestamp": "2026-10-22T09:00:00Z", "type": kind, "payload": payload})
                + "\n"
            )
    for i in range(100):
        with engine.source.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "timestamp": "2026-10-22T09:01:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": str(i),
                            "output": "verbose " + "x" * 7000,
                        },
                    }
                )
                + "\n"
            )
    engine.ingest()
    packet, _, _ = engine.packet(live=True)
    data = json.loads(packet)
    assert any(
        e["source_type"] == "turn_complete" and "602 теста" in e["summary"] for e in data["events"]
    )
    assert len(packet) <= PACKET_BUDGET
    assert all(e["kind"] != "reasoning" for e in data["events"])


@pytest.mark.asyncio
async def test_new_request_after_completed_turn_starts_fresh_task_list(engine):
    engine.ingest()
    ref = engine.repo.progress_events(engine.session_id, -1)[-1]["logical_event_id"]
    engine.assessor = AsyncMock(return_value=report_for(ref))
    await engine.assess(live=True)
    with engine.source.open("a") as stream:
        for typ, payload in [
            ("event_msg", {"type": "task_complete", "last_agent_message": "Готово"}),
            ("event_msg", {"type": "task_started"}),
            (
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"text": "Исправь новые замечания"}],
                },
            ),
        ]:
            stream.write(
                json.dumps({"timestamp": "2026-10-22T09:00:00Z", "type": typ, "payload": payload})
                + "\n"
            )
    engine.ingest()
    packet, _, previous = engine.packet(live=True)
    assert previous is None
    assert json.loads(packet)["previous_report"] is None
