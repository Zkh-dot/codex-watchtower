"""Incremental ingestion, bounded assessment and durable progress snapshots."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codex_watchtower.codex.discovery import read_session_meta, session_title
from codex_watchtower.codex.goal import reported_goal
from codex_watchtower.codex.normalize import normalize_record
from codex_watchtower.codex.tailer import IdentityBroken, read_new_records, resolve_start
from codex_watchtower.models.codex_cli import run_codex
from codex_watchtower.privacy.redact import redact_text
from codex_watchtower.report.contract import ProgressReport, validate_evidence
from codex_watchtower.storage.repository import Repository

PACKET_BUDGET = 48_000
MAX_REPORT_CHARACTERS = 24_000
LIVE_TIMEOUT = 60
WINDOW_MINUTES = 5
Assessor = Callable[[str, str], Awaitable[ProgressReport]]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ReportEngine:
    def __init__(
        self,
        repo: Repository,
        source: Path,
        output: Path,
        *,
        interval: int = 300,
        model: str = "gpt-6-luna",
        assessor: Assessor = run_codex,
        fast: bool = False,
    ) -> None:
        if not 300 <= interval <= 600:
            raise ValueError("interval must be between 300 and 600 seconds")
        self.repo = repo
        self.source = source.absolute()
        if self.source.is_symlink():
            raise ValueError("symlink sources are not supported")
        self.output = output.absolute()
        if (
            self.output.resolve() == self.source.resolve()
            or self.output.is_symlink()
            or self.output.suffix.lower() != ".html"
        ):
            raise ValueError("output must be a regular report file distinct from the source")
        self.interval = interval
        self.model = model
        self.assessor = assessor
        self.fast = fast
        self.analysis_mode: str | None = None
        meta = read_session_meta(self.source)
        session_id = meta.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("source must start with session_meta containing a session id")
        self.session_id = session_id
        self.session_title = session_title(self.source)
        self.session_workspace = redact_text(str(meta.get("cwd", ""))).text
        repo.upsert_session(
            session_id,
            workspace=str(meta.get("cwd", "")),
            started_at=str(meta.get("timestamp") or now_iso()),
            state="unknown",
        )
        repo.init_progress_source(session_id, str(self.source), self.source.stat().st_size)
        self.analyzing = False
        self.export_error: str | None = None
        self.source_error: str | None = None
        self.bootstrap_target: int | None = None

    def ingest(self) -> bool:
        cursor = resolve_start(self.source, self.repo.get_cursor(self.session_id))
        if isinstance(cursor, IdentityBroken):
            raise ValueError("Source changed before its checkpoint; restore its verified prefix")
        result = read_new_records(
            self.source,
            cursor,
            sessions_root=self.source.parent,
            expected_uid=os.getuid(),
            max_records=2000,
        )
        with self.repo.transaction():
            for record in result.records:
                normalized = normalize_record(self.session_id, record, fallback_timestamp=now_iso())
                if normalized is None:
                    continue
                event = normalized.event
                self.repo.insert_event_if_new(
                    self.session_id,
                    event.id,
                    kind=event.kind.value,
                    timestamp=event.timestamp,
                    summary=event.summary,
                    source_type=event.source_type,
                    exit_code=event.exit_code,
                    path=event.path,
                    locator=record.locator,
                )
            self.repo.save_cursor(self.session_id, result.cursor)
            state = self.repo.get_progress_source(self.session_id)
            assert state is not None
            self.repo.update_progress_source(
                self.session_id,
                last_attempt_at=state["last_attempt_at"],
                error=state["error"],
                malformed_delta=result.malformed_count,
            )
        state = self.repo.get_progress_source(self.session_id)
        assert state is not None
        if result.cursor.byte_offset >= state["bootstrap_bytes"]:
            self.repo.set_progress_bootstrap(
                self.session_id, self.repo.max_event_sequence(self.session_id)
            )
        state = self.repo.get_progress_source(self.session_id)
        assert state is not None
        self.bootstrap_target = state["bootstrap_target"]
        return result.cursor.byte_offset > cursor.byte_offset

    def packet(self, *, live: bool = False) -> tuple[str, int, ProgressReport | None]:
        latest = self.repo.latest_progress(self.session_id, live=live)
        if live and latest is None:
            latest = self.repo.latest_progress(self.session_id)
        previous = ProgressReport.model_validate_json(latest["body"]) if latest else None
        after = latest["event_cursor"] if latest else -1
        rows = self.repo.progress_events(self.session_id, after)
        window_start = None
        available = len(rows)
        if live:
            last = self.repo.connection.execute(
                "SELECT timestamp FROM normalized_events WHERE session_id=? "
                "ORDER BY event_sequence DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            if last is None:
                raise ValueError("no events available")
            end = datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00"))
            window_start = (end - timedelta(minutes=WINDOW_MINUTES)).isoformat()
            rows = list(
                self.repo.connection.execute(
                    "SELECT * FROM normalized_events WHERE session_id=? "
                    "AND julianday(timestamp)>=julianday(?) ORDER BY event_sequence DESC",
                    (self.session_id, window_start),
                )
            )
            available = len(rows)
        evidence = self.repo.progress_evidence(
            self.session_id, previous.references() if previous else set()
        )
        if live:
            # Keep the current request available even if it predates the five-minute window.
            anchor = self.repo.connection.execute(
                "SELECT * FROM normalized_events WHERE session_id=? AND kind='message' "
                "AND source_type='user' ORDER BY event_sequence DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            if anchor and anchor["logical_event_id"] not in {
                r["logical_event_id"] for r in evidence
            }:
                evidence.append(anchor)
        source_state = self.repo.get_progress_source(self.session_id)
        assert source_state is not None
        packet: dict[str, Any] = {
            "session_id": self.session_id,
            "previous_report": previous.model_dump() if previous else None,
            "previous_evidence": [
                {"id": row["logical_event_id"], "summary": row["summary"][: 1200 if live else 160]}
                for row in evidence
            ],
            "events": [],
            "coverage": {
                "mode": "recent" if live else "history",
                "window_minutes": WINDOW_MINUTES if live else None,
                "window_start": window_start,
                "available_events": available,
                "included_events": 0,
                "omitted_events": available,
                "after_sequence": after,
                "through_sequence": after,
                "latest_sequence": self.repo.max_event_sequence(self.session_id),
                "initial_history_fully_read": self.bootstrap_target is not None,
                "malformed_records": source_state["malformed_count"],
                "notes": "Only excerpts are available; unknown events and compaction are gaps.",
            },
        }

        def encode() -> str:
            return json.dumps(packet, ensure_ascii=False, separators=(",", ":"))

        if len(encode()) > PACKET_BUDGET - 5000:
            packet["previous_evidence"] = [
                {
                    "id": r["logical_event_id"],
                    **(
                        {"summary": r["summary"][:1200]}
                        if live and r["source_type"] == "user"
                        else {}
                    ),
                }
                for r in evidence
            ]
        for row in rows:
            event = {
                "id": row["logical_event_id"],
                "sequence": row["event_sequence"],
                "timestamp": row["timestamp"],
                "kind": row["kind"],
                "source_type": row["source_type"],
                "summary": row["summary"][:1200] if live else row["summary"],
                "exit_code": row["exit_code"],
            }
            packet["events"].append(event)
            packet["coverage"]["included_events"] = len(packet["events"])
            packet["coverage"]["omitted_events"] = available - len(packet["events"])
            packet["coverage"]["through_sequence"] = row["event_sequence"]
            if len(encode()) > PACKET_BUDGET:
                packet["events"].pop()
                break
        if not packet["events"]:
            raise ValueError("no new event fits the context budget")
        if live:
            packet["events"].reverse()
        cursor = packet["events"][-1]["sequence"]
        packet["coverage"]["included_events"] = len(packet["events"])
        packet["coverage"]["omitted_events"] = available - len(packet["events"])
        packet["coverage"]["through_sequence"] = cursor
        return encode(), cursor, previous

    def live_due(self, now: float) -> bool:
        if self.analyzing or self.source_error or self.bootstrap_target is None:
            return False
        latest = self.repo.latest_progress(self.session_id, live=True)
        maximum = self.repo.max_event_sequence(self.session_id)
        state = self.repo.get_progress_source(self.session_id)
        assert state is not None
        return bool(
            maximum is not None
            and (latest is None or latest["event_cursor"] < maximum)
            and (
                (latest is None and not state["live_error"])
                or now >= state["live_attempt_at"] + self.interval
            )
        )

    def due(self, now: float) -> bool:
        if self.analyzing or self.source_error or self.bootstrap_target is None:
            return False
        latest = self.repo.latest_progress(self.session_id)
        maximum = self.repo.max_event_sequence(self.session_id)
        after = latest["event_cursor"] if latest else -1
        if maximum is None or maximum <= after:
            return False
        state = self.repo.get_progress_source(self.session_id)
        assert state is not None
        if after >= self.bootstrap_target:
            return False
        # Initial history is folded once; subsequent work uses the recent window.
        return not state["error"] or now >= state["last_attempt_at"] + self.interval

    async def assess(self, *, live: bool = False) -> None:
        if self.analyzing:
            return
        self.analyzing = True
        self.analysis_mode = "recent" if live else "history"
        if live:
            self.repo.connection.execute(
                "UPDATE progress_sources SET live_attempt_at=?,live_error=NULL WHERE session_id=?",
                (time.time(), self.session_id),
            )
        else:
            self.repo.update_progress_source(
                self.session_id, last_attempt_at=time.time(), error=None
            )
        try:
            async with asyncio.timeout(LIVE_TIMEOUT):
                packet, cursor, previous = self.packet(live=live)
                if self.assessor is run_codex:
                    report = await run_codex(packet, self.model, fast=self.fast)
                else:
                    report = await self.assessor(packet, self.model)
            refs = {event["id"] for event in json.loads(packet)["events"]}
            refs.update(event["id"] for event in json.loads(packet)["previous_evidence"])
            validate_evidence(report, refs, previous)
            body = report.model_dump_json()
            if len(body) > MAX_REPORT_CHARACTERS:
                raise ValueError("report exceeds the 24000 character incremental context limit")
            # Redact string values before serialization so secret removal cannot break JSON.
            body = json.dumps(
                json.loads(
                    body,
                    object_hook=lambda obj: {
                        key: redact_text(value).text if isinstance(value, str) else value
                        for key, value in obj.items()
                    },
                ),
                ensure_ascii=False,
            )
            ProgressReport.model_validate_json(body)
            if live:
                self.repo.connection.execute(
                    "INSERT OR REPLACE INTO progress_live_reports "
                    "(session_id,event_cursor,assessed_at,body,coverage) VALUES (?,?,?,?,?)",
                    (
                        self.session_id,
                        cursor,
                        now_iso(),
                        body,
                        json.dumps(json.loads(packet)["coverage"]),
                    ),
                )
            else:
                self.repo.save_progress(self.session_id, cursor, now_iso(), body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = {
                "TimeoutError": "Модель превысила время ожидания",
                "ValidationError": "Ответ модели не соответствует схеме",
                "ValueError": "Неверные подтверждения или превышен бюджет контекста",
                "FileNotFoundError": "Codex CLI не найден в PATH",
                "RuntimeError": "Вызов модели через Codex CLI не выполнен",
            }.get(type(exc).__name__, "Не удалось выполнить анализ")
            error = f"{reason}. Последний успешный отчёт сохранён."
            if live:
                self.repo.connection.execute(
                    "UPDATE progress_sources SET live_attempt_at=?,live_error=? WHERE session_id=?",
                    (time.time(), error, self.session_id),
                )
            else:
                self.repo.update_progress_source(
                    self.session_id,
                    last_attempt_at=time.time(),
                    error=error,
                )
        finally:
            self.analyzing = False
            self.analysis_mode = None
            self.export()

    def snapshot(self) -> dict[str, Any]:
        archive = self.repo.latest_progress(self.session_id)
        live = self.repo.latest_progress(self.session_id, live=True)
        latest = live if live is not None else archive
        maximum = self.repo.max_event_sequence(self.session_id)
        state = self.repo.get_progress_source(self.session_id)
        assert state is not None
        report = json.loads(latest["body"]) if latest else None
        refs = ProgressReport.model_validate(report).references() if report else set()
        recent = self.repo.connection.execute(
            "SELECT timestamp FROM normalized_events WHERE session_id=? "
            "ORDER BY event_sequence DESC LIMIT 1",
            (self.session_id,),
        ).fetchone()
        counts = self.repo.connection.execute(
            "SELECT SUM(kind='unknown') AS unknown_count, "
            "SUM(summary LIKE '%[TRUNCATED event excerpt]%') AS truncated_count "
            "FROM normalized_events WHERE session_id=?",
            (self.session_id,),
        ).fetchone()
        turn = self.repo.connection.execute(
            "SELECT source_type, timestamp FROM normalized_events WHERE session_id=? "
            "AND kind='turn_lifecycle' ORDER BY event_sequence DESC LIMIT 1",
            (self.session_id,),
        ).fetchone()
        turn_start = self.repo.connection.execute(
            "SELECT event_sequence FROM normalized_events WHERE session_id=? "
            "AND kind='turn_lifecycle' AND source_type='turn_started' "
            "ORDER BY event_sequence DESC LIMIT 1",
            (self.session_id,),
        ).fetchone()
        goal = None
        goal_sequence = -1
        candidates = self.repo.connection.execute(
            "SELECT logical_event_id, event_sequence, timestamp, summary FROM normalized_events "
            "WHERE session_id=? AND kind='command_result' "
            "AND source_type IN ('response_item:function_call_output', "
            "'response_item:custom_tool_call_output', 'goal_status') "
            "AND (source_type='goal_status' OR summary LIKE '%goal%') "
            "ORDER BY event_sequence DESC",
            (self.session_id,),
        )
        for row in candidates:
            canonical = row["summary"].startswith("{")
            if canonical:
                try:
                    data = json.loads(row["summary"])
                except ValueError:
                    continue
                call_id = data.get("call_id") if isinstance(data, dict) else None
            else:
                call_id = row["summary"].partition("[")[2].partition("]: ")[0]
            if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                continue
            call = self.repo.connection.execute(
                "SELECT summary FROM normalized_events WHERE session_id=? AND kind='command' "
                "AND event_sequence<? AND instr(summary, ?)>0 ORDER BY event_sequence DESC LIMIT 1",
                (self.session_id, row["event_sequence"], f"[{call_id}]:"),
            ).fetchone()
            if not call or not any(
                marker in call["summary"]
                for marker in (
                    "tools.update_goal(",
                    "tools.get_goal(",
                    "update_goal [",
                    "get_goal [",
                )
            ):
                continue
            if goal := reported_goal(
                row["summary"] if canonical else row["summary"].partition("]: ")[2],
                self.session_id,
            ):
                goal.update({"at": row["timestamp"], "ref": row["logical_event_id"]})
                goal_sequence = row["event_sequence"]
                break
        analyzed = self.repo.connection.execute(
            "SELECT timestamp FROM normalized_events WHERE session_id=? AND event_sequence=?",
            (self.session_id, latest["event_cursor"] if latest else -1),
        ).fetchone()
        attempt_at = state["live_attempt_at"] or state["last_attempt_at"]
        return {
            "version": (archive["version"] if archive else 0) + (live["version"] if live else 0),
            "fast": self.fast,
            "window_minutes": WINDOW_MINUTES,
            "report_mode": "recent" if live else "history",
            "analysis_mode": self.analysis_mode,
            "window_coverage": json.loads(live["coverage"]) if live else None,
            "recent_events": [
                {"timestamp": r["timestamp"], "kind": r["kind"], "summary": r["summary"][:400]}
                for r in self.repo.connection.execute(
                    "SELECT timestamp,kind,summary FROM normalized_events WHERE session_id=? "
                    "AND kind='message' AND source_type IN ('assistant','user') "
                    "GROUP BY summary ORDER BY MAX(event_sequence) DESC LIMIT 3",
                    (self.session_id,),
                )
            ],
            "session_id": self.session_id,
            "session_title": self.session_title,
            "session_workspace": self.session_workspace,
            "report": report,
            "reported_goal": goal,
            "work_after_goal": bool(
                goal
                and goal["status"] == "complete"
                and turn_start
                and turn_start["event_sequence"] > goal_sequence
            ),
            "last_turn_at": turn["timestamp"] if turn else None,
            "report_through_at": analyzed["timestamp"] if analyzed else None,
            "report_is_current": bool(
                latest
                and (
                    latest["event_cursor"] == maximum
                    or (
                        live is not None
                        and time.time() - datetime.fromisoformat(live["assessed_at"]).timestamp()
                        <= self.interval + LIVE_TIMEOUT
                        and (not turn_start or live["event_cursor"] >= turn_start["event_sequence"])
                    )
                )
                and state["bootstrap_target"] is not None
                and not self.source_error
            ),
            "assessed_at": latest["assessed_at"] if latest else None,
            "last_event_at": recent["timestamp"] if recent else None,
            "last_turn_state": (
                "between_turns"
                if turn and turn["source_type"] == "turn_complete"
                else "active_turn"
                if turn and turn["source_type"] == "turn_started"
                else "unknown"
            ),
            "process_state": "unverified",
            "next_check_at": (
                datetime.fromtimestamp(attempt_at + self.interval, UTC)
                .replace(microsecond=0)
                .isoformat()
                if attempt_at
                and maximum is not None
                and (not latest or maximum > latest["event_cursor"])
                else None
            ),
            "analyzing": self.analyzing,
            "error": self.source_error
            or state["live_error"]
            or (state["error"] if not live else None),
            "export_error": self.export_error,
            "coverage": {
                "analyzed_sequence": archive["event_cursor"] if archive else -1,
                "latest_sequence": maximum,
                "malformed_count": state["malformed_count"],
                "unknown_count": counts["unknown_count"] or 0,
                "truncated_count": counts["truncated_count"] or 0,
                "initial_history_fully_read": state["bootstrap_target"] is not None,
                "history_target": state["bootstrap_target"],
                "history_complete": bool(
                    archive
                    and state["bootstrap_target"] is not None
                    and archive["event_cursor"] >= state["bootstrap_target"]
                ),
            },
            "evidence": [
                {
                    "id": r["logical_event_id"],
                    "timestamp": r["timestamp"],
                    "kind": r["kind"],
                    "summary": r["summary"],
                }
                for r in self.repo.progress_evidence(self.session_id, refs)
            ],
            "history": [
                {
                    "version": r["version"],
                    "at": r["assessed_at"],
                    "progress": json.loads(r["body"])["progress"],
                    "scope_changed": json.loads(r["body"])["scope_changed"],
                }
                for r in (
                    self.repo.connection.execute(
                        "SELECT * FROM progress_live_reports WHERE session_id=? ORDER BY version",
                        (self.session_id,),
                    )
                    if live
                    else self.repo.progress_history(self.session_id)
                )
            ],
        }

    def export(self) -> None:
        from codex_watchtower.report.web import export_html

        try:
            self.export_error = None
            export_html(self.output, self.snapshot())
        except OSError:
            self.export_error = "Не удалось сохранить HTML; файл отстаёт от текущего отчёта."

    async def watch(self) -> None:
        task: asyncio.Task[None] | None = None
        try:
            while True:
                advanced = False
                try:
                    advanced = self.ingest()
                    self.source_error = None
                except (OSError, ValueError, RuntimeError) as exc:
                    self.source_error = (
                        f"Источник недоступен или изменился ({type(exc).__name__}). "
                        "Анализ приостановлен до восстановления источника."
                    )
                if task is None or task.done():
                    if self.live_due(time.time()):
                        task = asyncio.create_task(self.assess(live=True))
                    elif self.due(time.time()):
                        task = asyncio.create_task(self.assess())
                self.export()
                await asyncio.sleep(
                    0 if self.bootstrap_target is None and advanced and not self.source_error else 5
                )
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
