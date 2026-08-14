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


def _split_sql_statements(sql: str) -> list[str]:
    """Split a migration script into individual statements.

    Handles ``--`` line comments and ``/* */`` block comments.
    Splits on semicolons that are not inside single-quoted strings.
    Empty/whitespace-only statements are discarded.
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    while i < len(sql):
        char = sql[i]
        # Handle line comments
        if (
            not in_string
            and not in_block_comment
            and char == "-"
            and i + 1 < len(sql)
            and sql[i + 1] == "-"
        ):
            in_line_comment = True
            current.append(char)
            i += 1
            continue
        if in_line_comment:
            current.append(char)
            if char == "\n":
                in_line_comment = False
            i += 1
            continue
        # Handle block comments
        if (
            not in_string
            and not in_line_comment
            and char == "/"
            and i + 1 < len(sql)
            and sql[i + 1] == "*"
        ):
            in_block_comment = True
            current.append(char)
            i += 1
            continue
        if in_block_comment:
            current.append(char)
            if char == "*" and i + 1 < len(sql) and sql[i + 1] == "/":
                current.append(sql[i + 1])
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        # Normal character
        current.append(char)
        if char == "'":
            in_string = not in_string
        elif char == ";" and not in_string:
            stmt = "".join(current).strip()
            if stmt and stmt != ";":
                statements.append(stmt)
            current = []
        i += 1
    remainder = "".join(current).strip()
    if remainder:
        statements.append(remainder)
    return statements


def migrate(conn: sqlite3.Connection) -> None:
    """Apply every migration under migrations/ that has not been recorded yet.

    Each migration is applied atomically: all statements in the migration
    plus the marker insert run in a single transaction, so a crash
    between statements rolls back the entire migration and it is retried
    cleanly on the next startup (R8#1).
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
        statements = _split_sql_statements(path.read_text())
        # Execute the migration and its marker in a single transaction
        # so a crash at any point rolls back the whole migration (R8#1).
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def open_database(path: Path) -> sqlite3.Connection:
    """The only supported way to obtain a Watchtower state connection.

    Corruption is detected before the file is ever opened for writing, and
    is reported as a typed, fatal error instead of silently recreating the
    database.
    """
    check_integrity(path)
    conn = connect(path)
    migrate(conn)
    return conn
