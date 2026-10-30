"""Back up and compact the OpenCode database while OpenCode is fully stopped."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import storage_backup
import storage_db
import storage_env
import storage_processes
import storage_render as render
import storage_v1


class CleanupError(Exception):
    """A cleanup precondition failed or an operation could not complete."""


def _backup_database(db_path: Path, backup: Path, to_backup: bool) -> None:
    if not to_backup:
        return
    try:
        storage_backup.backup_database(db_path, backup)
    except storage_backup.BackupError as error:
        raise CleanupError(str(error)) from error


def _archive_and_remove(paths: list[Path], archive: Path, to_backup: bool) -> None:
    try:
        storage_backup.archive_and_remove(paths, archive if to_backup else None)
    except storage_backup.BackupError as error:
        raise CleanupError(str(error)) from error


def build_plan(
    artifacts: storage_env.Artifacts,
    clear_events: bool,
    delete_orphan_diffs: bool,
    purge_v1: bool = False,
) -> dict:
    """Describe what cleanup would remove, without modifying anything."""
    plan = {
        "db_bytes": artifacts.db_path.stat().st_size
        if artifacts.db_path.is_file()
        else 0,
        "wal_bytes": artifacts.wal_path.stat().st_size
        if artifacts.wal_path.exists()
        else 0,
        "freelist_bytes": 0,
        "clear_events": {"enabled": bool(clear_events), "rows": 0, "bytes": 0},
        "orphan_diffs": {
            "enabled": bool(delete_orphan_diffs),
            "count": 0,
            "bytes": 0,
            "files": [],
        },
        "purge_v1": {"enabled": bool(purge_v1)},
        "estimated_reclaim_bytes": 0,
    }

    if artifacts.db_path.is_file():
        connection = storage_db.connect_readonly(artifacts.db_path)
        try:
            plan["freelist_bytes"] = storage_db.page_metrics(connection)[
                "freelist_bytes"
            ]
            live_sessions = storage_db.live_session_ids(connection)
            if clear_events and storage_db.table_exists(connection, "event"):
                plan["clear_events"]["rows"] = storage_db.scalar(
                    connection, "SELECT COUNT(*) FROM event"
                )
                plan["clear_events"]["bytes"] = storage_db.scalar(
                    connection, "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM event"
                )
            if purge_v1:
                plan["purge_v1"].update(storage_v1.residue(connection, artifacts))
        finally:
            connection.close()

        if delete_orphan_diffs and artifacts.diff_dir.is_dir():
            orphan_paths = [
                path
                for path in artifacts.diff_dir.glob("*.json")
                if path.stem not in live_sessions
            ]
            plan["orphan_diffs"]["files"] = [path.name for path in orphan_paths]
            plan["orphan_diffs"]["count"] = len(orphan_paths)
            plan["orphan_diffs"]["bytes"] = sum(
                path.stat().st_size for path in orphan_paths
            )

    plan["estimated_reclaim_bytes"] = (
        plan["freelist_bytes"]
        + plan["clear_events"]["bytes"]
        + plan["orphan_diffs"]["bytes"]
        + plan["purge_v1"].get("bytes", 0)
        + plan["purge_v1"].get("diffs", {}).get("bytes", 0)
    )
    return plan


def run(
    artifacts: storage_env.Artifacts,
    clear_events: bool,
    delete_orphan_diffs: bool,
    backup_dir: Path,
    dry_run: bool = False,
    to_backup: bool = False,
    purge_v1: bool = False,
    no_process_check: bool = False,
) -> dict:
    """Back up the database, optionally clean it, and VACUUM it back to the filesystem."""
    state = storage_processes.detect()
    plan = build_plan(artifacts, clear_events, delete_orphan_diffs, purge_v1)
    use = storage_processes.file_in_use(artifacts.db_path)
    file_use = {"in_use": use.in_use, "checked": use.checked, "detail": use.detail}
    if dry_run:
        return {
            "dry_run": True,
            "plan": plan,
            "processes": state.processes,
            "process_check": {"checked": state.checked, "detail": state.detail},
            "file_use": file_use,
            "backup": bool(to_backup),
            "backup_dir": str(Path(backup_dir).expanduser()),
        }

    if state.processes:
        detail = "\n".join(
            f"  PID {entry['pid']}: {entry['command']}" for entry in state.processes
        )
        raise CleanupError(
            "OpenCode is running; VACUUM requires exclusive access.\n"
            f"{detail}\nExit every OpenCode serve/attach process, then run this command again."
        )
    if use.in_use:
        raise CleanupError(
            f"the database is open in another process ({use.detail}); VACUUM requires "
            "exclusive access.\nExit every OpenCode serve/attach process, then run this "
            "command again."
        )
    if not state.checked or not use.checked:
        reasons = [
            detail
            for detail, checked in ((state.detail, state.checked), (use.detail, use.checked))
            if not checked
        ]
        if not no_process_check:
            raise CleanupError(
                "cannot confirm that OpenCode is stopped: "
                + "; ".join(reasons)
                + ".\nStop it by hand, then pass --no-process-check to continue."
            )
    if not artifacts.db_path.is_file():
        raise CleanupError(f"database not found: {artifacts.db_path}")
    backup_dir = Path(backup_dir).expanduser()
    if not backup_dir.is_dir():
        raise CleanupError(f"backup directory not found: {backup_dir}")
    if purge_v1 and plan["purge_v1"].get("reason"):
        raise CleanupError(f"--purge-v1 refused: {plan['purge_v1']['reason']}")

    stamp = storage_backup.timestamp()
    backup = backup_dir / f"opencode.db.pre-cleanup-{stamp}.bak"
    before = artifacts.db_path.stat().st_size
    _backup_database(artifacts.db_path, backup, to_backup)

    events = {"deleted": False, "rows": 0, "bytes": 0}
    v1 = {"deleted": False, "sessions": 0, "tables": {}}
    purged_ids: set[str] = set()
    live_sessions: set[str] = set()
    integrity = "unknown"
    connection = sqlite3.connect(artifacts.db_path, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise CleanupError(f"pre-VACUUM quick_check failed: {check}")

        if clear_events and storage_db.table_exists(connection, "event"):
            events["rows"] = storage_db.scalar(connection, "SELECT COUNT(*) FROM event")
            events["bytes"] = storage_db.scalar(
                connection, "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM event"
            )
            connection.execute("DELETE FROM event")
            # V2 numbers new session_message rows from event_sequence; clearing it
            # makes them collide with the UNIQUE (session_id, seq) index.
            if storage_db.table_exists(
                connection, "event_sequence"
            ) and not storage_db.is_v2_layout(connection):
                connection.execute("DELETE FROM event_sequence")
            connection.commit()
            events["deleted"] = True

        if purge_v1:
            try:
                removed = storage_v1.purge(connection)
            except storage_v1.PurgeError as error:
                raise CleanupError(f"--purge-v1 refused: {error}") from error
            purged_ids = removed["session_ids"]
            v1 = {"deleted": True, "sessions": len(purged_ids), "tables": removed["tables"]}

        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CleanupError(f"post-VACUUM integrity_check failed: {integrity}")
        live_sessions = storage_db.live_session_ids(connection)
    finally:
        connection.close()

    orphans = {"deleted": False, "count": 0, "bytes": 0, "archive": None}
    if delete_orphan_diffs and artifacts.diff_dir.is_dir():
        orphan_paths = [
            path
            for path in artifacts.diff_dir.glob("*.json")
            if path.stem not in live_sessions
        ]
        if orphan_paths:
            archive = backup_dir / f"opencode-orphan-session-diffs-{stamp}.zip"
            bytes_total = sum(path.stat().st_size for path in orphan_paths)
            _archive_and_remove(orphan_paths, archive, to_backup)
            orphans = {
                "deleted": True,
                "count": len(orphan_paths),
                "bytes": bytes_total,
                "archive": str(archive) if to_backup else None,
            }

    v1["diffs"] = {"count": 0, "bytes": 0, "archive": None}
    v1_diff_paths = storage_v1.diff_paths(artifacts, purged_ids)
    if v1_diff_paths:
        archive = backup_dir / f"opencode-v1-session-diffs-{stamp}.zip"
        bytes_total = sum(path.stat().st_size for path in v1_diff_paths)
        _archive_and_remove(v1_diff_paths, archive, to_backup)
        v1["diffs"] = {
            "count": len(v1_diff_paths),
            "bytes": bytes_total,
            "archive": str(archive) if to_backup else None,
        }

    after = artifacts.db_path.stat().st_size
    wal_after = artifacts.wal_path.stat().st_size if artifacts.wal_path.exists() else 0

    backup_bytes = backup.stat().st_size if to_backup else 0

    return {
        "dry_run": False,
        "process_check": {"checked": state.checked, "detail": state.detail},
        "file_use": file_use,
        "before_bytes": before,
        "after_bytes": after,
        "wal_bytes": wal_after,
        "reclaimed_bytes": before - after,
        "backup_path": str(backup) if to_backup else None,
        "backup_bytes": backup_bytes,
        "events": events,
        "orphans": orphans,
        "v1": v1,
        "integrity": integrity,
        "freelist_bytes": plan["freelist_bytes"],
    }


def render_plan(
    plan: dict,
    state: storage_processes.ProcessState,
    use: storage_processes.FileUse,
    backup_dir: str,
    console: render.Console,
    backup: bool = False,
) -> None:
    console.section("Preflight")
    if state.processes:
        console.write(
            "  "
            + console.yellow(f"! {len(state.processes)} OpenCode process(es) running")
            + console.dim("  (fine for a dry run; blocks a real VACUUM)")
        )
    elif use.in_use:
        console.write(
            "  "
            + console.yellow("! the database is open in another process")
            + console.dim(f"  ({use.detail})")
        )
    elif not state.checked or not use.checked:
        detail = "; ".join(
            item
            for item, checked in ((state.detail, state.checked), (use.detail, use.checked))
            if not checked
        )
        console.write(
            "  "
            + console.yellow("! cannot check for OpenCode processes")
            + console.dim(f"  ({detail})")
        )
    else:
        console.write("  " + console.green("✓") + " no OpenCode processes running")
    found = plan["db_bytes"] > 0
    console.write(
        "  "
        + (console.green("✓") if found else console.red("✗"))
        + f" database {'found' if found else 'missing'}"
        + console.dim(f"  {render.human_bytes(plan['db_bytes'])}")
    )

    console.section("Planned changes")
    table = render.Table(
        [("what", "left", "bold"), ("amount", "left", ""), ("flag", "left", "dim")]
    )
    events = plan["clear_events"]
    table.add(
        "event rows",
        f"{render.human_count(events['rows'])} rows · {render.human_bytes(events['bytes'])}",
        "--clear-events" if events["enabled"] else "(not enabled)",
    )
    orphans = plan["orphan_diffs"]
    table.add(
        "orphan diffs",
        f"{render.human_count(orphans['count'])} files · {render.human_bytes(orphans['bytes'])}",
        "--delete-orphan-diffs" if orphans["enabled"] else "(not enabled)",
    )
    v1 = plan["purge_v1"]
    if not v1["enabled"]:
        table.add("V1 residue", "-", "(not enabled)")
    elif v1.get("reason"):
        table.add("V1 residue", console.red(f"refused: {v1['reason']}"), "--purge-v1")
    else:
        table.add(
            "V1 residue",
            f"{render.human_count(v1['sessions'])} sessions · "
            f"{render.human_count(v1['rows'])} rows · {render.human_bytes(v1['bytes'])}",
            "--purge-v1",
        )
        table.add(
            "V1 diffs",
            f"{render.human_count(v1['diffs']['count'])} files · "
            f"{render.human_bytes(v1['diffs']['bytes'])}",
            "--purge-v1",
        )
    table.add("freelist pages", render.human_bytes(plan["freelist_bytes"]), "VACUUM")
    table.add(
        "reclaimable total",
        console.green(render.human_bytes(plan["estimated_reclaim_bytes"])),
        "",
    )
    table.write(console)
    if v1["enabled"] and v1.get("unmigrated_sessions"):
        console.write(
            "  "
            + console.yellow(
                f"! {v1['unmigrated_sessions']} V1 sessions are not in V2 and will be kept"
            )
        )
    console.write("")
    if backup:
        console.write(
            console.dim("  backup would be written to ") + console.dim(str(backup_dir))
        )
    else:
        console.write(
            "  " + console.yellow("! no backup will be written") + console.dim("  (pass --backup)")
        )
    console.section("Dry run")
    console.write("  " + console.yellow("nothing was modified"))


def render_result(result: dict, console: render.Console) -> None:
    console.section("Preflight")
    check = result.get("process_check") or {"checked": True}
    use = result.get("file_use") or {"checked": True}
    if check["checked"] and use["checked"]:
        console.write("  " + console.green("✓") + " no OpenCode processes running")
    else:
        console.write(
            "  "
            + console.yellow("! process check skipped")
            + console.dim("  (--no-process-check)")
        )

    console.section("Backup")
    if result["backup_path"]:
        console.write(
            "  "
            + console.green("✓")
            + f" {render.human_bytes(result['backup_bytes'])} written"
            + console.dim(f"  {result['backup_path']}")
        )
        console.write("  " + console.green("✓") + " backup integrity_check ok")
    else:
        console.write(
            "  " + console.yellow("! no backup written") + console.dim("  (pass --backup to keep one)")
        )

    console.section("Cleanup")
    events = result["events"]
    if events["deleted"]:
        console.write(
            "  "
            + console.green("✓")
            + f" {render.human_count(events['rows'])} event rows deleted"
            + console.dim(f"  {render.human_bytes(events['bytes'])}")
        )
    else:
        console.write("  " + console.dim("· events left untouched"))
    v1 = result["v1"]
    if v1["deleted"]:
        rows = sum(v1["tables"].values())
        console.write(
            "  "
            + console.green("✓")
            + f" V1 rows of {render.human_count(v1['sessions'])} migrated sessions deleted"
            + console.dim(f"  {render.human_count(rows)} rows")
        )
        console.write(
            "  "
            + console.green("✓")
            + f" {v1['diffs']['count']} V1 session diffs removed"
            + console.dim(f"  {v1['diffs']['archive'] or 'no archive'}")
        )
    else:
        console.write("  " + console.dim("· V1 residue left untouched"))
    console.write("  " + console.green("✓") + " WAL checkpointed and VACUUMed")
    console.write("  " + console.green("✓") + f" integrity_check={result['integrity']}")
    orphans = result["orphans"]
    if orphans["deleted"]:
        console.write(
            "  "
            + console.green("✓")
            + f" {orphans['count']} orphan diffs removed"
            + console.dim(f"  {orphans['archive'] or 'no archive'}")
        )
    else:
        console.write("  " + console.dim("· orphan diffs left untouched"))

    console.section("Result")
    before = result["before_bytes"]
    after = result["after_bytes"]
    reclaimed = result["reclaimed_bytes"]
    fraction = reclaimed / before if before else 0.0
    console.write(
        f"  {render.human_bytes(before)} → "
        + console.bold(render.human_bytes(after))
        + console.dim(f"   WAL {render.human_bytes(result['wal_bytes'])}")
    )
    console.write(
        "  "
        + console.green(render.human_bytes(reclaimed))
        + f" reclaimed ({render.ratio_text(fraction)})  "
        + console.green(render.bar(fraction))
    )
    console.write("")
    if result["backup_path"]:
        console.write(
            console.dim(
                "  delete the backup only after OpenCode starts and retained sessions open"
            )
        )
