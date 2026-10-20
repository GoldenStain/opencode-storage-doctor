"""Delete one session, with its child sessions, chosen by id or unique prefix."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import storage_backup
import storage_db
import storage_env
import storage_render as render
import storage_v1


FORMATS = ("auto", "v1", "v2")
OPENCODE_TIMEOUT_SECONDS = 120


class SessionDeleteError(Exception):
    """The session could not be resolved, or a deletion step failed or was refused."""


def _candidates(connection: sqlite3.Connection) -> list[dict]:
    """Return every session id known to V2 or V1, with its title, V2 data first."""
    sessions: dict[str, dict] = {}
    for table in ("session_v2", "session"):
        if not storage_db.table_exists(connection, table):
            continue
        for session_id, title, directory in connection.execute(
            f"SELECT id, title, directory FROM {table}"
        ):
            sessions.setdefault(
                session_id,
                {"id": session_id, "title": (title or "").replace("\n", " ").strip(), "directory": directory or ""},
            )
    return list(sessions.values())


def _subtree(connection: sqlite3.Connection, root: str) -> list[str]:
    """Return the root id and all descendant ids, following parent_id in V2 and V1."""
    edges = " UNION ".join(
        f"SELECT id, parent_id FROM {table}"
        for table in ("session_v2", "session")
        if storage_db.table_exists(connection, table)
    )
    rows = connection.execute(
        f"WITH RECURSIVE edge(id, parent_id) AS ({edges}), "
        "tree(id) AS (SELECT ? UNION SELECT edge.id FROM edge JOIN tree ON edge.parent_id = tree.id) "
        "SELECT id FROM tree",
        (root,),
    )
    return [row[0] for row in rows]


def _in(column: str) -> str:
    return f"{column} IN (SELECT value FROM json_each(?))"


def build_plan(artifacts: storage_env.Artifacts, query: str, fmt: str) -> dict:
    """Resolve the session and describe what each deletion step would remove."""
    if fmt not in FORMATS:
        raise SessionDeleteError(f"unknown format {fmt!r}")
    if not artifacts.db_path.is_file():
        raise SessionDeleteError(f"database not found: {artifacts.db_path}")
    connection = storage_db.connect_readonly(artifacts.db_path)
    try:
        if not storage_db.is_v2_layout(connection):
            raise SessionDeleteError("session delete supports OpenCode V2 databases only")
        matched, matches = storage_db.resolve_session(_candidates(connection), query)
        if matched is None:
            if not matches:
                raise SessionDeleteError(f"no session matching {query!r}")
            listed = "\n".join(f"  {item['id']}  {item['title'] or '(untitled)'}" for item in matches[:10])
            raise SessionDeleteError(f"prefix {query!r} matches {len(matches)} sessions:\n{listed}")

        ids = _subtree(connection, matched["id"])
        parameter = (json.dumps(ids),)
        v2_ids = [row[0] for row in connection.execute(f"SELECT id FROM session_v2 WHERE {_in('id')}", parameter)]
        v2_messages, v2_bytes = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(data AS BLOB))), 0) "
            f"FROM session_message WHERE {_in('session_id')}",
            parameter,
        ).fetchone()
        v2_created = connection.execute(
            "SELECT time_created FROM session_v2 WHERE id=?", (matched["id"],)
        ).fetchone()

        v1_ids: list[str] = []
        only_copy: list[str] = []
        if storage_db.table_exists(connection, "session"):
            v1_ids = [row[0] for row in connection.execute(f"SELECT id FROM session WHERE {_in('id')}", parameter)]
            # V1 rows are the only copy when V2 lacks the session or all of its messages.
            only_copy = [
                row[0]
                for row in connection.execute(
                    f"SELECT s.id FROM session s WHERE {_in('s.id')} AND NOT EXISTS "
                    "(SELECT 1 FROM session_message x WHERE x.session_id = s.id) "
                    "AND EXISTS (SELECT 1 FROM message m WHERE m.session_id = s.id)",
                    parameter,
                )
            ]
        v1 = storage_v1.session_residue(connection, artifacts, ids)
        v1_reason = storage_v1.unavailable_reason(connection)
    finally:
        connection.close()

    in_v2 = matched["id"] in v2_ids
    has_v1 = bool(v1["rows"] or v1["diffs"]["count"])
    if fmt == "v2" and not in_v2:
        raise SessionDeleteError(f"{matched['id']} does not exist in V2")
    if fmt == "v1" and not has_v1:
        raise SessionDeleteError(f"{matched['id']} has no V1 residue")
    steps = {"v2": fmt in ("auto", "v2") and in_v2, "v1": fmt in ("auto", "v1") and has_v1}
    if not any(steps.values()):
        raise SessionDeleteError(f"nothing to delete for {matched['id']}")
    if steps["v1"] and v1_reason:
        raise SessionDeleteError(f"V1 residue cannot be deleted: {v1_reason}")
    current = os.environ.get("OPENCODE_SESSION_ID")
    if current and current in ids:
        raise SessionDeleteError(f"refusing to delete the session running this command ({current})")

    return {
        "query": query,
        "format": fmt,
        "session": matched,
        "session_ids": ids,
        "steps": steps,
        "v2": {
            "present": in_v2,
            "sessions": len(v2_ids),
            "session_ids": v2_ids,
            "messages": v2_messages,
            "bytes": v2_bytes,
            "created": v2_created[0] if v2_created else None,
        },
        "v1": {**v1, "sessions": len(v1_ids), "only_copy": only_copy},
    }


def _same_store(artifacts: storage_env.Artifacts) -> bool:
    default = storage_env.Artifacts(storage_env.resolve_data_dir(None))
    return artifacts.db_path.resolve() == default.db_path.resolve()


def _opencode(arguments: list[str]) -> subprocess.CompletedProcess:
    executable = shutil.which("opencode")
    if not executable:
        raise SessionDeleteError("opencode is not on PATH; V2 deletion needs the OpenCode CLI")
    return subprocess.run(
        [executable, *arguments],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=OPENCODE_TIMEOUT_SECONDS,
    )


def delete_v2(artifacts: storage_env.Artifacts, plan: dict) -> None:
    """Delete the session tree through `opencode session delete`, then verify it in the database."""
    session_id = plan["session"]["id"]
    # The CLI deletes from the service's database, not from --data-dir.
    if not _same_store(artifacts):
        raise SessionDeleteError(
            "V2 deletion goes through the OpenCode service, which does not use "
            f"{artifacts.db_path}; use --format v1 or the default data directory"
        )
    probe = _opencode(["api", "get", f"/api/session/{session_id}"])
    try:
        created = json.loads(probe.stdout)["data"]["time"]["created"]
    except (ValueError, KeyError, TypeError):
        created = None
    if probe.returncode != 0 or created != plan["v2"]["created"]:
        raise SessionDeleteError(
            f"the OpenCode service does not see {session_id} as this database does; nothing deleted"
        )

    result = _opencode(["session", "delete", session_id])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SessionDeleteError(f"opencode session delete failed: {detail}")

    connection = storage_db.connect_readonly(artifacts.db_path)
    try:
        left = storage_db.scalar(
            connection,
            f"SELECT COUNT(*) FROM session_v2 WHERE {_in('id')}",
            (json.dumps(plan["v2"]["session_ids"]),),
        )
    finally:
        connection.close()
    if left:
        raise SessionDeleteError(f"{left} V2 sessions still exist after opencode session delete")


def delete_v1(artifacts: storage_env.Artifacts, plan: dict, archive: Path | None) -> dict:
    """Delete V1 rows and session_diff files of the session tree."""
    connection = sqlite3.connect(artifacts.db_path, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        removed = storage_v1.purge_session(connection, plan["session_ids"])
    except storage_v1.PurgeError as error:
        raise SessionDeleteError(f"V1 residue cannot be deleted: {error}") from error
    finally:
        connection.close()
    paths = storage_v1.diff_paths(artifacts, set(plan["session_ids"]))
    diff_bytes = sum(path.stat().st_size for path in paths)
    if paths:
        storage_backup.archive_and_remove(paths, archive)
    return {
        "tables": removed["tables"],
        "diffs": {
            "count": len(paths),
            "bytes": diff_bytes,
            "archive": str(archive) if archive is not None and paths else None,
        },
    }


def execute(artifacts: storage_env.Artifacts, plan: dict, to_backup: bool, backup_dir: Path) -> dict:
    """Run the planned steps: optional backup, then V2, then V1."""
    backup_dir = Path(backup_dir).expanduser()
    stamp = storage_backup.timestamp()
    result = {"backup_path": None, "v2_deleted": False, "v1": None}
    if to_backup:
        if not backup_dir.is_dir():
            raise SessionDeleteError(f"backup directory not found: {backup_dir}")
        backup = backup_dir / f"opencode.db.pre-session-delete-{stamp}.bak"
        try:
            storage_backup.backup_database(artifacts.db_path, backup)
        except storage_backup.BackupError as error:
            raise SessionDeleteError(str(error)) from error
        result["backup_path"] = str(backup)

    if plan["steps"]["v2"]:
        delete_v2(artifacts, plan)
        result["v2_deleted"] = True
    if plan["steps"]["v1"]:
        archive = backup_dir / f"opencode-session-diffs-{stamp}.zip" if to_backup else None
        try:
            result["v1"] = delete_v1(artifacts, plan, archive)
        except SessionDeleteError as error:
            if result["v2_deleted"]:
                raise SessionDeleteError(f"V2 session deleted, but V1 step failed: {error}") from error
            raise
    return result


def render_plan(plan: dict, console: render.Console) -> None:
    session = plan["session"]
    console.section("Session")
    console.line("id", session["id"])
    console.line("title", render.truncate(session["title"], 60) or "(untitled)")
    console.line("directory", session["directory"] or "-")
    children = len(plan["session_ids"]) - 1
    if children:
        console.line("children", f"{children} child sessions are deleted with it")

    console.section("Planned changes")
    table = render.Table([("step", "left", "bold"), ("amount", "left", ""), ("how", "left", "dim")])
    v2 = plan["v2"]
    if plan["steps"]["v2"]:
        table.add(
            "V2 session",
            f"{v2['sessions']} sessions · {render.human_count(v2['messages'])} messages · "
            f"{render.human_bytes(v2['bytes'])}",
            "opencode session delete",
        )
    else:
        table.add("V2 session", "-", "not in V2" if not v2["present"] else "(not selected)")
    v1 = plan["v1"]
    if plan["steps"]["v1"]:
        table.add(
            "V1 residue",
            f"{v1['sessions']} sessions · {render.human_count(v1['rows'])} rows · "
            f"{render.human_bytes(v1['bytes'])}",
            "direct SQL delete",
        )
        table.add(
            "V1 diffs",
            f"{v1['diffs']['count']} files · {render.human_bytes(v1['diffs']['bytes'])}",
            "storage/session_diff",
        )
    else:
        table.add("V1 residue", "-", "none" if not v1["rows"] and not v1["diffs"]["count"] else "(not selected)")
    table.write(console)
    if plan["steps"]["v1"] and v1["only_copy"]:
        console.write(
            "  "
            + console.yellow(f"! {len(v1['only_copy'])} sessions exist only as V1 data; deleting it loses them")
        )


def render_result(plan: dict, result: dict, console: render.Console) -> None:
    console.section("Result")
    if result["backup_path"]:
        console.write("  " + console.green("✓") + " backup written" + console.dim(f"  {result['backup_path']}"))
    else:
        console.write("  " + console.yellow("! no backup written"))
    if result["v2_deleted"]:
        console.write("  " + console.green("✓") + f" {plan['v2']['sessions']} V2 sessions deleted")
    v1 = result["v1"]
    if v1 is not None:
        console.write(
            "  "
            + console.green("✓")
            + f" {render.human_count(sum(v1['tables'].values()))} V1 rows deleted"
        )
        console.write(
            "  "
            + console.green("✓")
            + f" {v1['diffs']['count']} diff files removed"
            + console.dim(f"  {v1['diffs']['archive'] or 'no archive'}")
        )
    console.write("")
    console.write(
        console.dim("  deleted rows only free pages; run `audit`, then `cleanup` with OpenCode stopped to VACUUM")
    )
