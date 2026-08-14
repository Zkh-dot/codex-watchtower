"""SQLite connection, WAL setup, migrations, and corruption-safe startup.

Task 4/5 of the implementation plan: state persists in SQLite/WAL, and a
database that fails ``PRAGMA quick_check`` must refuse to start rather than
being silently recreated (spec section 8, "State database corruption").
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class DatabaseCorruptionError(RuntimeError):
    """Raised when the state database exists but fails integrity checks."""


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def connect(path: Path) -> sqlite3.Connection:
    """Open a read/write connection with WAL mode and explicit-transaction semantics."""
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def check_integrity(path: Path) -> None:
    """Verify the on-disk file before anything opens it read/write.

    Runs over an immutable ``file:`` URI so the probe itself can never
    create ``-wal``/``-shm`` sidecars next to a database it is only meant
    to inspect.
    """
    if not path.exists():
        return
    uri = f"file:{path.resolve()}?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorruptionError(
                f"state database at {path} is not a readable SQLite file: {exc}"
            ) from exc
    finally:
        conn.close()
    if row is None or row[0] != "ok":
        detail = row[0] if row is not None else "no result"
        raise DatabaseCorruptionError(
            f"state database at {path} failed PRAGMA quick_check: {detail}"
        )


def migrate(conn: sqlite3.Connection) -> None:
    """Apply every migration under migrations/ that has not been recorded yet.

    Each migration is idempotent (CREATE TABLE/INDEX IF NOT EXISTS) and the
    applied set is also tracked explicitly, so running this twice against
    the same database is a no-op the second time either way.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  filename TEXT PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ")"
    )
    applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}
    for path in _migration_files():
        if path.name in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,))


def open_database(path: Path) -> sqlite3.Connection:
    """The only supported way to obtain a Watchtower state connection.

    Corruption is detected before the file is ever opened for writing, and
    is reported as a typed, fatal error instead of silently recreating the
    database.

    After migration, abandoned model-call reservations (rows with
    ``finished_at IS NULL`` older than 5 minutes) are recovered so stale
    slots from a crashed process don't permanently block assessments (R5#1).
    """
    check_integrity(path)
    conn = connect(path)
    migrate(conn)
    # Recover abandoned reservations: delete model_calls rows that were
    # reserved but never finalized due to a crash/SIGKILL/restart.
    conn.execute(
        """
        DELETE FROM model_calls
        WHERE finished_at IS NULL
          AND started_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-300 seconds')
        """
    )
    return conn
