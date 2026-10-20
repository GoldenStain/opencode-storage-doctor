"""Clear leftover V1 conversation data out of an OpenCode V2 database."""

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
# An explicit list of session ids, passed as one JSON array parameter.
IDS_SQL = "SELECT value FROM json_each(?)"


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


def _page_bytes(connection: sqlite3.Connection, table: str) -> int | None:
    """Return on-disk bytes of a table plus its indexes, or None without dbstat."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name=? AND type IN ('table','index')",
            (table,),
        )
    ]
    try:
        return sum(
            storage_db.scalar(connection, "SELECT SUM(pgsize) FROM dbstat WHERE name=?", (name,))
            for name in names
        )
    except sqlite3.OperationalError:
        return None


def _measure(connection: sqlite3.Connection, source: str, parameters=()) -> dict:
    """Estimate rows and on-disk bytes that deleting the sessions in source would free.

    Each table's pages (with indexes) are scaled by the removed share of its byte
    length, or of its row count when it has no large column. Without dbstat the
    estimate falls back to the removed byte length.
    """
    result = {"tables": {}, "rows": 0, "bytes": 0}
    for table in TABLES:
        if not storage_db.table_exists(connection, table):
            continue
        column = BYTES_COLUMN.get(table)
        length = f"LENGTH(CAST({column} AS BLOB))" if column else "1"
        selected = _session_filter(table, source)
        total_rows, rows, total_length, removed_length = connection.execute(
            f"SELECT COUNT(*), COALESCE(SUM({selected}), 0), COALESCE(SUM({length}), 0), "
            f"COALESCE(SUM(CASE WHEN {selected} THEN {length} END), 0) FROM {table}",
            tuple(parameters) * 2,
        ).fetchone()
        pages = _page_bytes(connection, table) if rows else 0
        if pages is None:
            size = removed_length if column else 0
        elif rows == total_rows:
            size = pages
        else:
            size = int(pages * removed_length / total_length) if total_length else 0
        result["tables"][table] = {"rows": rows, "bytes": size}
        result["rows"] += rows
        result["bytes"] += size
    return result


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
    result.update(_measure(connection, MIGRATED_SQL))

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
    return _purge(connection, MIGRATED_SQL)


def session_residue(
    connection: sqlite3.Connection, artifacts: storage_env.Artifacts, session_ids: list[str]
) -> dict:
    """Count V1 rows and session_diff files of the given sessions, migrated or not."""
    result = {"tables": {}, "rows": 0, "bytes": 0}
    if storage_db.table_exists(connection, "session"):
        result = _measure(connection, IDS_SQL, (json.dumps(session_ids),))
    paths = diff_paths(artifacts, set(session_ids))
    result["diffs"] = {"count": len(paths), "bytes": sum(path.stat().st_size for path in paths)}
    return result


def purge_session(connection: sqlite3.Connection, session_ids: list[str]) -> dict:
    """Delete V1 rows of the given sessions in one transaction; return what was removed."""
    return _purge(connection, IDS_SQL, (json.dumps(session_ids),))


def _purge(connection: sqlite3.Connection, source: str, parameters=()) -> dict:
    reason = unavailable_reason(connection)
    if reason is not None:
        raise PurgeError(reason)
    if not storage_db.table_exists(connection, "session"):
        return {"session_ids": set(), "tables": {}}

    if connection.in_transaction:
        connection.commit()
    # IMMEDIATE takes the write lock now, so no other writer can change the
    # V2 counts checked below, even while OpenCode is running.
    connection.execute("BEGIN IMMEDIATE")
    try:
        before = _v2_counts(connection)
        connection.execute("DROP TABLE IF EXISTS temp.purge_v1")
        connection.execute("CREATE TEMP TABLE purge_v1 (id TEXT PRIMARY KEY)")
        connection.execute(
            f"INSERT OR IGNORE INTO temp.purge_v1 SELECT * FROM ({source})", tuple(parameters)
        )
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
