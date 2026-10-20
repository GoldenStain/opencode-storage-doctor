"""Open OpenCode's SQLite database read-only and query which tables hold its sessions."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def scalar(connection: sqlite3.Connection, sql: str, parameters=()) -> int:
    row = connection.execute(sql, parameters).fetchone()
    return row[0] if row and row[0] is not None else 0


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def is_v2_layout(connection: sqlite3.Connection) -> bool:
    """Return True when the database holds OpenCode V2 session tables."""
    return table_exists(connection, "session_v2") and table_exists(
        connection, "session_message"
    )


def session_tables(connection: sqlite3.Connection) -> tuple[str, str, str | None]:
    """Return the (session, message, part) tables holding live conversations; V2 has no part table."""
    if is_v2_layout(connection):
        return "session_v2", "session_message", None
    return "session", "message", "part"


def live_session_ids(connection: sqlite3.Connection) -> set[str]:
    session_table = session_tables(connection)[0]
    return {row[0] for row in connection.execute(f"SELECT id FROM {session_table}")}


def resolve_session(sessions: list[dict], query: str) -> tuple[dict | None, list[dict]]:
    """Match a session by exact id or unique prefix, returning it plus any ambiguity."""
    exact = [session for session in sessions if session["id"] == query]
    if exact:
        return exact[0], []
    matches = [session for session in sessions if session["id"].startswith(query)]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def page_metrics(connection: sqlite3.Connection) -> dict:
    """Return SQLite page geometry and freelist bytes for the database."""
    page_size = scalar(connection, "PRAGMA page_size")
    page_count = scalar(connection, "PRAGMA page_count")
    freelist_pages = scalar(connection, "PRAGMA freelist_count")
    return {
        "page_size": page_size,
        "page_count": page_count,
        "physical_bytes": page_size * page_count,
        "freelist_bytes": page_size * freelist_pages,
    }