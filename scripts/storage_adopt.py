"""Reassign V1 "global" sessions to the V2 project of their plain directory."""

from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
import sqlite3
from pathlib import Path

import storage_backup
import storage_db
import storage_env
import storage_processes
import storage_render as render


GLOBAL = "global"
REQUIRED_COLUMNS = {"id", "project_id", "workspace_id", "parent_id", "directory", "path", "time_created", "time_updated"}


class AdoptError(Exception):
    """Global sessions could not be reassigned, or the database is not in a supported state."""


def project_id(directory: str) -> str:
    """Return the id V2 gives a directory outside any VCS tree.

    V2 hashes the OS-native path, so a directory read from a Windows database
    must be hashed with backslashes even when this script runs on POSIX.
    """
    return hashlib.sha1(f"directory:{storage_env.native_dir(directory)}".encode()).hexdigest()


def project_id_candidates(directory: str) -> list[str]:
    """Return the ids OpenCode may have written for a directory, native form first.

    Older V2 builds hashed the stored path verbatim, which after the path
    normalization migration is the slash form on Windows.
    """
    candidates = []
    for form in (storage_env.native_dir(directory), str(directory).replace("\\", "/")):
        value = hashlib.sha1(f"directory:{form}".encode()).hexdigest()
        if value not in candidates:
            candidates.append(value)
    return candidates


def _path_module(directory: str):
    return ntpath if storage_env.looks_windows(directory) else posixpath


def _vcs_marker(directory: str) -> str | None:
    current = storage_env.native_dir(directory)
    path = _path_module(current)
    while True:
        for name in (".git", ".hg"):
            marker = path.join(current, name)
            if os.path.lexists(marker):
                return marker
        parent = path.dirname(current)
        if parent == current:
            return None
        current = parent


def _skip_reason(directory: str) -> str | None:
    if not storage_env.is_absolute_dir(directory) or not storage_env.is_normalized_dir(directory):
        return "path is not absolute and normalized"
    local = storage_env.native_dir(directory)
    if not os.path.isdir(local):
        return "directory does not exist"
    real = os.path.realpath(local)
    if not storage_env.same_path(real, directory):
        return f"symlinked (real path {real})"
    marker = _vcs_marker(directory)
    if marker:
        # V2 itself adopts global sessions into a git or hg project when it opens one.
        return f"inside a VCS tree ({marker}); OpenCode adopts these itself"
    return None


def _existing_project(connection: sqlite3.Connection, directory: str):
    """Return the project row for a directory under any id form OpenCode may use."""
    for candidate in project_id_candidates(directory):
        row = connection.execute(
            "SELECT id, worktree, vcs FROM project WHERE id = ?", (candidate,)
        ).fetchone()
        if row is not None:
            return row
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
    if row[0] not in project_id_candidates(row[1]):
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
        existing = _existing_project(connection, directory)
        target = existing[0] if existing else project_id(directory)
        if reason is None and existing and (
            not storage_env.same_path(existing[1], directory) or existing[2] is not None
        ):
            reason = f"project {target} exists with worktree {existing[1]} and vcs {existing[2]}"
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
            if not storage_env.same_path(worktree, move["directory"]) or vcs is not None:
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


def execute(
    artifacts: storage_env.Artifacts,
    plan: dict,
    to_backup: bool,
    backup_dir: Path,
    no_process_check: bool = False,
) -> dict:
    """Refuse while OpenCode runs, optionally back up, then apply the plan."""
    if plan["reason"]:
        raise AdoptError(plan["reason"])
    state = storage_processes.detect()
    use = storage_processes.file_in_use(artifacts.db_path)
    if state.processes:
        detail = "\n".join(f"  PID {entry['pid']}: {entry['command']}" for entry in state.processes)
        raise AdoptError(
            "OpenCode is running; it keeps session locations in memory.\n"
            f"{detail}\nStop every OpenCode process (for example `opencode service stop`), then run this again."
        )
    if use.in_use:
        raise AdoptError(
            f"the database is open in another process ({use.detail}); it keeps session "
            "locations in memory.\nStop every OpenCode process, then run this again."
        )
    if not state.checked or not use.checked:
        reasons = [
            detail
            for detail, checked in ((state.detail, state.checked), (use.detail, use.checked))
            if not checked
        ]
        if not no_process_check:
            raise AdoptError(
                "cannot confirm that OpenCode is stopped: "
                + "; ".join(reasons)
                + ".\nStop it by hand, then pass --no-process-check."
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
