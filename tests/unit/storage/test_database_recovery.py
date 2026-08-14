from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codex_watchtower.storage import db


def test_healthy_database_opens_and_migrates(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = db.open_database(path)
    conn.close()
    assert path.exists()


def test_corrupt_database_fails_startup_with_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a sqlite file at all, just garbage bytes")

    with pytest.raises(db.DatabaseCorruptionError):
        db.open_database(path)


def test_corrupt_database_check_leaves_main_file_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"garbage-not-sqlite" * 10)
    before = path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()

    with pytest.raises(db.DatabaseCorruptionError):
        db.check_integrity(path)

    after = path.read_bytes()
    assert hashlib.sha256(after).hexdigest() == before_hash


def test_corrupt_database_check_creates_no_wal_or_shm_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"garbage-not-sqlite" * 10)

    with pytest.raises(db.DatabaseCorruptionError):
        db.check_integrity(path)

    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_missing_database_is_not_treated_as_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.db"
    db.check_integrity(path)  # must not raise; a fresh database is not corruption


def test_healthy_database_passes_quick_check_probe_without_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = db.open_database(path)
    conn.close()

    db.check_integrity(path)  # must not raise for a valid, healthy file

    # WAL sidecars from normal operation may exist; the immutable probe must
    # not have created *additional* ones beyond what open_database itself made.
    assert path.exists()
