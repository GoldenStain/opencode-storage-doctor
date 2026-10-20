"""Measure and delete V1 conversation data that OpenCode V2 has already migrated."""

from __future__ import annotations

import json
import sqlite3

import storage_db
import storage_env


# Children before parents. V2 defines no model for any of these tables; it reads
# them only in its one-time V1->V2 migration. Rows are deleted, tables are kept,
# so later upstream migrations that touch these tables still find them.
TABLES = ("part", "message", "todo", "session_share", "session")
V1_PARENTS = ("session", "message", "part")
KEY_COLUMN = {"session": "id"}

# A V1 session counts as migrated when V2 holds the session and, if V1 had any
# messages for it, at least one V2 message. Everything else is kept.
MIGRATED_SQL = """
    SELECT s.id FROM session s
     WHERE EXISTS (SELECT 1 FROM session_v2 v WHERE v.id = s.id)
       AND (NOT EXISTS (SELECT 1 FROM message m WHERE m.session_id = s.id)
            OR EXISTS (SELECT 1 FROM session_message x WHERE x.session_id = s.id))
"""
BYTES_COLUMN = {"message": "data", "part": "data", "todo": "content"}


class PurgeError(Exception):
    """The V1 purge found the database in a state it does not handle."""


def _unexpected_referrers(connection: sqlite3.Connection) -> list[str]:
    """Return tables outside TABLES whose foreign keys point at V1 rows."""
    names = [
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    found = []
    for name in names:
        if name in TABLES:
            continue
        targets = {row[2] for row in connection.execute(f'PRAGMA foreign_key_list("{name}")')}
        if targets & set(V1_PARENTS):
            found.append(name)
    return found


def unavailable_reason(connection: sqlite3.Connection) -> str | None:
    """Explain why a purge must not run, or return None when it may."""
    if not storage_db.is_v2_layout(connection):
        return "not a V2 database"
    if not storage_db.table_exists(connection, "session"):
        return None
    row = None
    if storage_db.table_exists(connection, "kv"):
        row = connection.execute(
            "SELECT value FROM kv WHERE key='migration.v1-v2'"
        ).fetchone()
    try:
        phase = json.loads(row[0]).get("phase") if row else None
    except (TypeError, ValueError, AttributeError):
        phase = None
    if phase != "completed":
        return "V2 has not completed its V1->V2 migration (kv migration.v1-v2)"
    referrers = _unexpected_referrers(connection)
    if referrers:
        return f"unexpected tables reference V1 rows: {', '.join(referrers)}"
    return None


def _session_filter(table: str, source: str) -> str:
    return f"{KEY_COLUMN.get(table, 'session_id')} IN ({source})"


def residue(connection: sqlite3.Connection, artifacts: storage_env.Artifacts) -> dict:
    """Count V1 rows and session_diff files that a purge would remove."""
    result = {
        "available": False,
        "reason": unavailable_reason(connection),
        "sessions": 0,
        "unmigrated_sessions": 0,
        "tables": {},
        "rows": 0,
        "bytes": 0,
        "diffs": {"count": 0, "bytes": 0},
    }
    if result["reason"] is not None or not storage_db.table_exists(connection, "session"):
        return result
    result["available"] = True

    migrated = {row[0] for row in connection.execute(MIGRATED_SQL)}
    result["sessions"] = len(migrated)
    result["unmigrated_sessions"] = (
        storage_db.scalar(connection, "SELECT COUNT(*) FROM session") - len(migrated)
    )
    for table in TABLES:
        if not storage_db.table_exists(connection, table):
            continue
        column = BYTES_COLUMN.get(table)
        size = f"COALESCE(SUM(LENGTH({column})), 0)" if column else "0"
        rows, size_bytes = connection.execute(
            f"SELECT COUNT(*), {size} FROM {table} WHERE {_session_filter(table, MIGRATED_SQL)}"
        ).fetchone()
        result["tables"][table] = {"rows": rows, "bytes": size_bytes}
        result["rows"] += rows
        result["bytes"] += size_bytes

    paths = diff_paths(artifacts, migrated)
    result["diffs"] = {"count": len(paths), "bytes": sum(path.stat().st_size for path in paths)}
    return result


def diff_paths(artifacts: storage_env.Artifacts, session_ids: set[str]) -> list:
    """Return session_diff files of the given sessions; V2 never reads these files."""
    if not artifacts.diff_dir.is_dir():
        return []
    return [path for path in artifacts.diff_dir.glob("*.json") if path.stem in session_ids]


def _v2_counts(connection: sqlite3.Connection) -> tuple[int, int]:
    return (
        storage_db.scalar(connection, "SELECT COUNT(*) FROM session_v2"),
        storage_db.scalar(connection, "SELECT COUNT(*) FROM session_message"),
    )


def purge(connection: sqlite3.Connection) -> dict:
    """Delete V1 rows of migrated sessions in one transaction; return what was removed."""
    reason = unavailable_reason(connection)
    if reason is not None:
        raise PurgeError(reason)
    if not storage_db.table_exists(connection, "session"):
        return {"session_ids": set(), "tables": {}}

    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        before = _v2_counts(connection)
        connection.execute("DROP TABLE IF EXISTS temp.purge_v1")
        connection.execute(f"CREATE TEMP TABLE purge_v1 AS {MIGRATED_SQL}")
        session_ids = {row[0] for row in connection.execute("SELECT id FROM temp.purge_v1")}
        deleted = {}
        for table in TABLES:
            if storage_db.table_exists(connection, table):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE {_session_filter(table, 'SELECT id FROM temp.purge_v1')}"
                )
                deleted[table] = cursor.rowcount
        # Guards against cascades or triggers reaching the V2 tables.
        after = _v2_counts(connection)
        if after != before:
            raise PurgeError(f"V2 row counts changed during purge: {before} -> {after}")
        connection.execute("DROP TABLE temp.purge_v1")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"session_ids": session_ids, "tables": deleted}
