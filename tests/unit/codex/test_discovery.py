from __future__ import annotations

import json
from pathlib import Path

from codex_watchtower.codex.discovery import discover_sessions


def _write(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_missing_root_returns_empty_list(tmp_path: Path) -> None:
    assert discover_sessions(tmp_path / "does-not-exist") == []


def test_discovers_nested_yyyy_mm_dd_layout(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write(
        root / "2026" / "08" / "13" / "rollout-a.jsonl",
        [{"type": "session_meta", "payload": {"id": "sess-a", "cwd": "/w/a"}}],
    )
    _write(
        root / "2026" / "08" / "14" / "rollout-b.jsonl",
        [{"type": "session_meta", "payload": {"id": "sess-b", "cwd": "/w/b"}}],
    )
    found = discover_sessions(root)
    ids = {s.session_id for s in found}
    assert ids == {"sess-a", "sess-b"}


def test_ignores_non_rollout_files(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "13" / "rollout-a.jsonl", [{"type": "session_meta"}])
    other = root / "2026" / "08" / "13" / "notes.txt"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("not a rollout")
    found = discover_sessions(root)
    assert len(found) == 1
    assert found[0].path.name == "rollout-a.jsonl"


def test_discovery_is_ordered_by_path() -> None:
    fixtures_root = Path(__file__).resolve().parents[2] / "fixtures" / "codex" / "healthy"
    found = discover_sessions(fixtures_root)
    paths = [s.path for s in found]
    assert paths == sorted(paths)


def test_extracts_session_metadata_from_healthy_fixture() -> None:
    fixtures_root = Path(__file__).resolve().parents[2] / "fixtures" / "codex" / "healthy"
    found = discover_sessions(fixtures_root)
    assert len(found) == 1
    session = found[0]
    assert session.session_id == "sess-healthy-1"
    assert session.workspace == "/home/user/project"
    assert session.started_at == "2026-08-13T10:00:00Z"
    assert session.model == "gpt-5-codex"
    assert session.metadata_complete is True


def test_missing_session_meta_preserved_as_unknown_not_a_crash(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "13" / "rollout-broken.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not even json\n")
    found = discover_sessions(root)
    assert len(found) == 1
    session = found[0]
    assert session.metadata_complete is False
    assert session.session_id == "rollout-broken"  # falls back to filename stem
    assert session.workspace is None


def test_empty_file_preserved_as_unknown(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "13" / "rollout-empty.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    found = discover_sessions(root)
    assert len(found) == 1
    assert found[0].metadata_complete is False
