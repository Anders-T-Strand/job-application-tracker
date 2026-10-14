"""SQLite helpers and a minimal file-based migration runner."""

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending .sql files from app/migrations in name order. Returns what ran."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  name TEXT PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
    ran: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name.startswith(".") or path.name in applied:
            continue  # dotfiles: e.g. macOS "._foo.sql" AppleDouble artifacts
        conn.executescript(path.read_text())
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (path.name,))
        conn.commit()
        ran.append(path.name)
    return ran


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM sync_state WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()
