"""Reassign V1 "global" sessions to the V2 project of their plain directory."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import storage_backup
import storage_db
import storage_env
import storage_render as render


GLOBAL = "global"
REQUIRED_COLUMNS = {"id", "project_id", "workspace_id", "parent_id", "directory", "path", "time_created", "time_updated"}


class AdoptError(Exception):
    """Global sessions could not be reassigned, or the database is not in a supported state."""


def project_id(directory: str) -> str:
    """Return the id V2 gives a directory outside any VCS tree."""
    return hashlib.sha1(f"directory:{directory}".encode()).hexdigest()


def _vcs_marker(directory: str) -> str | None:
    current = directory
    while True:
        for name in (".git", ".hg"):
            marker = os.path.join(current, name)
            if os.path.lexists(marker):
                return marker
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _skip_reason(directory: str) -> str | None:
    if not os.path.isabs(directory) or os.path.normpath(directory) != directory:
        return "path is not absolute and normalized"
    if not os.path.isdir(directory):
        return "directory does not exist"
    if os.path.realpath(directory) != directory:
        return f"symlinked (real path {os.path.realpath(directory)})"
    marker = _vcs_marker(directory)
    if marker:
        # V2 itself adopts global sessions into a git or hg project when it opens one.
        return f"inside a VCS tree ({marker}); OpenCode adopts these itself"
    return None


def unavailable_reason(connection: sqlite3.Connection) -> str | None:
    """Explain why sessions must not be reassigned, or return None when they may."""
    if not storage_db.is_v2_layout(connection) or not storage_db.table_exists(connection, "project"):
        return "not a V2 database"
    columns = {row[1] for row in connection.execute("PRAGMA table_info(session_v2)")}
    if not REQUIRED_COLUMNS <= columns:
        return f"session_v2 lacks columns {sorted(REQUIRED_COLUMNS - columns)}"
    # The id rule was read from the V2 binary. Check it against the newest row V2 wrote.
    row = connection.execute(
        "SELECT id, worktree FROM project WHERE vcs IS NULL AND id != ? "
        "ORDER BY time_created DESC LIMIT 1",
        (GLOBAL,),
    ).fetchone()
    if row is None:
        return (
            "no V2 directory project exists to confirm the project-id rule; "
            "open OpenCode once in any non-git directory, then retry"
        )
    if row[0] != project_id(row[1]):
        return f"project {row[0]} ({row[1]}) does not follow the known project-id rule; unsupported OpenCode version"
    return None


def build_plan(connection: sqlite3.Connection, only_dirs: list[str] | None = None) -> dict:
    """Group global sessions by directory and decide which directories can move."""
    plan = {"reason": unavailable_reason(connection), "moves": [], "skipped": [], "unknown_dirs": []}
    if not storage_db.is_v2_layout(connection) or not storage_db.table_exists(connection, "project"):
        return plan
    wanted = set(only_dirs or [])
    rows = connection.execute(
        "SELECT directory, COUNT(*), SUM(parent_id IS NULL), MIN(time_created), MAX(time_updated) "
        "FROM session_v2 WHERE project_id = ? AND workspace_id IS NULL "
        "GROUP BY directory ORDER BY COUNT(*) DESC",
        (GLOBAL,),
    ).fetchall()
    for directory, sessions, top_level, first, last in rows:
        if wanted and directory not in wanted:
            continue
        item = {"directory": directory, "sessions": sessions, "top_level": top_level, "first": first, "last": last}
        reason = _skip_reason(directory)
        target = project_id(directory)
        existing = connection.execute("SELECT worktree, vcs FROM project WHERE id = ?", (target,)).fetchone()
        if reason is None and existing and (existing[0] != directory or existing[1] is not None):
            reason = f"project {target} exists with worktree {existing[0]} and vcs {existing[1]}"
        if reason:
            plan["skipped"].append({**item, "reason": reason})
        else:
            plan["moves"].append({**item, "project_id": target, "project_exists": bool(existing)})
    plan["unknown_dirs"] = sorted(wanted - {row[0] for row in rows})
    plan["sessions"] = sum(item["sessions"] for item in plan["moves"])
    return plan


def apply(connection: sqlite3.Connection, plan: dict) -> dict:
    """Create missing directory projects and move the planned sessions in one transaction."""
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        reason = unavailable_reason(connection)
        if reason:
            raise AdoptError(reason)
        moved = {}
        for move in plan["moves"]:
            # Mirrors V2's own worktree.resolved projector: project_id plus an
            # empty subpath. time_updated is kept, so session order does not change.
            connection.execute(
                "INSERT OR IGNORE INTO project (id, worktree, vcs, time_created, time_updated, time_active, sandboxes) "
                "VALUES (?, ?, NULL, ?, ?, ?, '[]')",
                (move["project_id"], move["directory"], move["first"], move["last"], move["last"]),
            )
            worktree, vcs = connection.execute(
                "SELECT worktree, vcs FROM project WHERE id = ?", (move["project_id"],)
            ).fetchone()
            if worktree != move["directory"] or vcs is not None:
                raise AdoptError(f"project {move['project_id']} has worktree {worktree} and vcs {vcs}")
            cursor = connection.execute(
                "UPDATE session_v2 SET project_id = ?, path = '' "
                "WHERE project_id = ? AND workspace_id IS NULL AND directory = ?",
                (move["project_id"], GLOBAL, move["directory"]),
            )
            if cursor.rowcount != move["sessions"]:
                raise AdoptError(
                    f"{move['directory']}: planned {move['sessions']} sessions, found {cursor.rowcount}"
                )
            moved[move["directory"]] = cursor.rowcount
        broken = connection.execute("PRAGMA foreign_key_check(session_v2)").fetchall()
        if broken:
            raise AdoptError(f"foreign_key_check failed: {broken[:5]}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return moved


def execute(artifacts: storage_env.Artifacts, plan: dict, to_backup: bool, backup_dir: Path) -> dict:
    """Refuse while OpenCode runs, optionally back up, then apply the plan."""
    if plan["reason"]:
        raise AdoptError(plan["reason"])
    processes = storage_env.running_processes()
    if processes:
        detail = "\n".join(f"  PID {entry['pid']}: {entry['command']}" for entry in processes)
        raise AdoptError(
            "OpenCode is running; it keeps session locations in memory.\n"
            f"{detail}\nStop every OpenCode process (for example `opencode service stop`), then run this again."
        )
    result = {"backup_path": None, "moved": {}}
    if to_backup:
        backup_dir = Path(backup_dir).expanduser()
        if not backup_dir.is_dir():
            raise AdoptError(f"backup directory not found: {backup_dir}")
        backup = backup_dir / f"opencode.db.pre-adopt-global-{storage_backup.timestamp()}.bak"
        try:
            storage_backup.backup_database(artifacts.db_path, backup)
        except storage_backup.BackupError as error:
            raise AdoptError(str(error)) from error
        result["backup_path"] = str(backup)
    connection = sqlite3.connect(artifacts.db_path, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        result["moved"] = apply(connection, plan)
    finally:
        connection.close()
    return result


def render_plan(plan: dict, console: render.Console) -> None:
    console.section("Global sessions")
    if plan["reason"]:
        console.write("  " + console.red(f"✗ refused: {plan['reason']}"))
    if not plan["moves"] and not plan["skipped"]:
        console.write("  " + console.dim("no global sessions"))
    table = render.Table([("action", "left", "bold"), ("sessions", "right", ""), ("directory", "left", "")])
    for move in plan["moves"]:
        state = "existing" if move["project_exists"] else "new"
        table.add(
            "move",
            f"{move['sessions']} ({move['top_level']} top)",
            move["directory"],
            below=f"→ {state} project {move['project_id']}",
        )
    for item in plan["skipped"]:
        table.add("skip", f"{item['sessions']} ({item['top_level']} top)", item["directory"], below=item["reason"])
    if table.rows:
        table.write(console)
    for directory in plan["unknown_dirs"]:
        console.warning(f"  no global sessions in {directory}")


def render_result(plan: dict, result: dict, console: render.Console) -> None:
    console.section("Result")
    if result["backup_path"]:
        console.write("  " + console.green("✓") + " backup written" + console.dim(f"  {result['backup_path']}"))
    else:
        console.write("  " + console.yellow("! no backup written"))
    total = sum(result["moved"].values())
    console.write("  " + console.green("✓") + f" {total} sessions moved into {len(result['moved'])} directory projects")
    console.write("")
    console.write(console.dim("  start OpenCode, then run `opencode session list` in each moved directory"))


def summary(plan: dict) -> dict:
    """Return the counts the audit report shows."""
    return {
        "reason": plan["reason"],
        "movable_sessions": plan.get("sessions", 0),
        "movable_dirs": len(plan["moves"]),
        "skipped_sessions": sum(item["sessions"] for item in plan["skipped"]),
        "skipped_dirs": len(plan["skipped"]),
    }
