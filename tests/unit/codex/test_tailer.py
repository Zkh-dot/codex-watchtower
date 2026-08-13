from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path

import pytest

from codex_watchtower.codex import tailer
from codex_watchtower.storage.repository import CursorState


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fresh_cursor(path: Path) -> CursorState:
    resolved = tailer.resolve_start(path, None)
    assert isinstance(resolved, CursorState)
    return resolved


# --- complete-line-only reading --------------------------------------


def test_partial_final_line_produces_no_event_and_no_cursor_advance(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"type": "a"}\n{"type": "partial", "text": "not fini')
    cursor = _fresh_cursor(path)
    result = tailer.read_new_records(path, cursor)
    assert len(result.records) == 1
    assert result.trailing_partial == b'{"type": "partial", "text": "not fini'
    assert result.cursor.byte_offset == len(b'{"type": "a"}\n')


def test_completing_the_partial_line_yields_exactly_one_more_event(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"type": "a"}\n{"type": "partial"')
    cursor = _fresh_cursor(path)
    first = tailer.read_new_records(path, cursor)
    assert len(first.records) == 1

    with path.open("ab") as f:
        f.write(b"}\n")

    second = tailer.read_new_records(path, first.cursor)
    assert len(second.records) == 1
    assert second.records[0].parsed == {"type": "partial"}
    assert second.records[0].record_ordinal == 1


def test_multiple_complete_lines_all_yielded_in_order(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n{"n": 1}\n{"n": 2}\n')
    cursor = _fresh_cursor(path)
    result = tailer.read_new_records(path, cursor)
    assert [r.parsed["n"] for r in result.records] == [0, 1, 2]
    assert [r.record_ordinal for r in result.records] == [0, 1, 2]


def test_malformed_but_newline_terminated_line_is_quarantined_not_crashed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\nnot json at all\n{"n": 2}\n')
    cursor = _fresh_cursor(path)
    result = tailer.read_new_records(path, cursor)
    assert result.malformed_count == 1
    assert [r.record_ordinal for r in result.records] == [0, 1, 2]
    assert result.records[1].parsed is None


# --- restart from a stored byte cursor --------------------------------


def test_restart_from_stored_cursor_reads_only_new_bytes(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n{"n": 1}\n')
    cursor = _fresh_cursor(path)
    first = tailer.read_new_records(path, cursor)
    assert len(first.records) == 2

    with path.open("ab") as f:
        f.write(b'{"n": 2}\n')

    resumed = tailer.resolve_start(path, first.cursor)
    assert isinstance(resumed, CursorState)
    second = tailer.read_new_records(path, resumed)
    assert [r.parsed["n"] for r in second.records] == [2]
    assert second.records[0].record_ordinal == 2


def test_restart_with_no_new_bytes_yields_no_records(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n')
    cursor = _fresh_cursor(path)
    first = tailer.read_new_records(path, cursor)
    resumed = tailer.resolve_start(path, first.cursor)
    assert isinstance(resumed, CursorState)
    second = tailer.read_new_records(path, resumed)
    assert second.records == []
    assert second.cursor == first.cursor


# --- append-only precondition: identity and prefix verification -------


def test_prefix_preserving_rename_resumes_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n')
    cursor = _fresh_cursor(path)
    first = tailer.read_new_records(path, cursor)

    # Simulate a filesystem event that changes the inode but preserves
    # content exactly (e.g. a hardlink-based rotation): rewrite the same
    # bytes plus an appended line via a fresh inode (copy + append).
    new_path = tmp_path / "rollout.jsonl"
    data = new_path.read_bytes() + b'{"n": 1}\n'
    new_path.unlink()
    _write(new_path, data)  # new inode, same prefix bytes plus new content

    resumed = tailer.resolve_start(new_path, first.cursor)
    assert isinstance(resumed, CursorState)
    result = tailer.read_new_records(new_path, resumed)
    assert [r.parsed["n"] for r in result.records] == [1]
    assert result.records[0].record_ordinal == 1


def test_truncation_is_detected_as_identity_broken(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n{"n": 1}\n')
    cursor = _fresh_cursor(path)
    first = tailer.read_new_records(path, cursor)

    # Truncate below the checkpoint offset.
    _write(path, b'{"n": 0}\n')

    resumed = tailer.resolve_start(path, first.cursor)
    assert isinstance(resumed, tailer.IdentityBroken)
    assert "shorter" in resumed.detail or "truncation" in resumed.detail


def test_copy_truncate_with_changed_prefix_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n{"n": 1}\n')
    cursor = _fresh_cursor(path)
    first = tailer.read_new_records(path, cursor)

    # Same length-ish, but different content before the checkpoint: a
    # classic copy-truncate where the tool rewrote history instead of
    # appending to it.
    _write(path, b'{"n": 9}\n{"n": 1}\n')

    resumed = tailer.resolve_start(path, first.cursor)
    assert isinstance(resumed, tailer.IdentityBroken)
    assert "mismatch" in resumed.detail


def test_inode_reuse_by_unrelated_file_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n')
    cursor = _fresh_cursor(path)
    first = tailer.read_new_records(path, cursor)

    path.unlink()
    _write(path, b'{"totally": "different"}\n')  # unrelated content, maybe reused inode

    resumed = tailer.resolve_start(path, first.cursor)
    assert isinstance(resumed, tailer.IdentityBroken)


def test_fast_regrowth_past_old_offset_with_different_content_is_detected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n{"n": 1}\n')
    cursor = _fresh_cursor(path)
    first = tailer.read_new_records(path, cursor)

    # File shrinks then regrows past the old byte offset with different
    # content -- a pure size check would miss this; the content hash must not.
    _write(path, b'{"n": 0}\n{"different": true}\n{"n": 2}\n{"n": 3}\n')

    resumed = tailer.resolve_start(path, first.cursor)
    assert isinstance(resumed, tailer.IdentityBroken)


def test_empty_checkpoint_always_matches_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write(path, b'{"n": 0}\n')
    zero_cursor = CursorState(
        device=0, inode=0, byte_offset=0, record_ordinal=0, checkpoint_hash="anything"
    )
    resolved = tailer.resolve_start(path, zero_cursor)
    assert isinstance(resolved, CursorState)
    assert resolved.byte_offset == 0
    assert resolved.record_ordinal == 0


# --- open_safe: ownership/type/path/size checks ------------------------


def test_open_safe_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "outside.jsonl"
    _write(outside, b'{"n": 0}\n')
    link = root / "rollout.jsonl"
    link.symlink_to(outside)
    with pytest.raises(tailer.TailerSafetyError):
        tailer.open_safe(link, root)


def test_open_safe_rejects_non_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    fifo_path = root / "rollout.jsonl"
    os.mkfifo(fifo_path)
    try:
        with pytest.raises(tailer.TailerSafetyError):
            tailer.open_safe(fifo_path, root)
    finally:
        fifo_path.unlink()


def test_open_safe_rejects_wrong_owner(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "rollout.jsonl"
    _write(path, b'{"n": 0}\n')
    with pytest.raises(tailer.TailerSafetyError):
        tailer.open_safe(path, root, expected_uid=os.getuid() + 12345)


def test_open_safe_rejects_oversize_file(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "rollout.jsonl"
    _write(path, b'{"n": 0}\n')
    with pytest.raises(tailer.TailerSafetyError):
        tailer.open_safe(path, root, max_file_size=1)


def test_open_safe_accepts_healthy_file(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "rollout.jsonl"
    _write(path, b'{"n": 0}\n')
    fd = tailer.open_safe(path, root, expected_uid=os.getuid())
    try:
        st = os.fstat(fd)
        assert stat_module.S_ISREG(st.st_mode)
    finally:
        os.close(fd)


def test_read_new_records_uses_open_safe_when_sessions_root_given(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "outside.jsonl"
    _write(outside, b'{"n": 0}\n')
    link = root / "rollout.jsonl"
    link.symlink_to(outside)

    cursor = CursorState(
        device=0, inode=0, byte_offset=0, record_ordinal=0, checkpoint_hash=tailer.EMPTY_PREFIX_HASH
    )
    with pytest.raises(tailer.TailerSafetyError):
        tailer.read_new_records(link, cursor, sessions_root=root)
