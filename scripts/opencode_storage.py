#!/usr/bin/env python3
"""Command-line entry point for auditing and cleaning OpenCode's local storage."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import storage_adopt
import storage_audit
import storage_cleanup
import storage_db
import storage_env
import storage_processes
import storage_render
import storage_session
import storage_session_tree


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="OpenCode data directory (default: XDG data home)",
    )
    common.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="disable ANSI color (NO_COLOR is also honored)",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit machine-readable JSON instead of a rendered report",
    )

    parser = argparse.ArgumentParser(
        prog="opencode_storage",
        description="Audit and clean OpenCode's local storage.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser(
        "audit", parents=[common], help="read-only storage and database report"
    )
    audit.add_argument(
        "--top", type=int, default=15, help="sessions to list (default: 15)"
    )
    audit.add_argument(
        "--oversized-mib",
        type=float,
        default=10.0,
        help="message size warning threshold in MiB (default: 10)",
    )
    audit.add_argument(
        "--session",
        metavar="ID",
        default=None,
        help="show a focused report for one session by full id or unique prefix",
    )

    cleanup = subparsers.add_parser(
        "cleanup",
        parents=[common],
        help="back up, optionally clean, and VACUUM the database",
    )
    cleanup.add_argument(
        "--clear-events",
        action="store_true",
        help="delete event rows (and event_sequence on V1 databases) before VACUUM",
    )
    cleanup.add_argument(
        "--delete-orphan-diffs",
        action="store_true",
        help="archive then delete session_diff files without a live session",
    )
    cleanup.add_argument(
        "--purge-v1",
        action="store_true",
        help="delete V1 session/message/part/todo/session_share rows and session_diff "
        "files of sessions already migrated to V2",
    )
    cleanup.add_argument(
        "--backup-dir",
        type=Path,
        default=Path.home(),
        help="directory for timestamped backups (default: home directory)",
    )
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without modifying anything",
    )
    cleanup.add_argument(
        "--backup",
        action="store_true",
        help="write a verified database backup and zip removed diff files (default: no backup)",
    )
    cleanup.add_argument(
        "--no-process-check",
        action="store_true",
        help="continue when OpenCode processes cannot be listed (stop them by hand first)",
    )

    session = subparsers.add_parser("session", parents=[common], help="act on sessions")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    delete = session_commands.add_parser(
        "delete",
        parents=[common],
        help="delete one session and its child sessions from V2 and/or V1 data",
    )
    delete.add_argument("session_id", metavar="ID", help="full session id or unique prefix")
    delete.add_argument(
        "--format",
        choices=storage_session.FORMATS,
        default="auto",
        help="v2: opencode session delete; v1: V1 rows and diff files; auto: both that exist",
    )
    delete.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    delete.add_argument(
        "--backup",
        action="store_true",
        help="write a verified database backup and zip removed diff files first",
    )
    delete.add_argument(
        "--backup-dir",
        type=Path,
        default=Path.home(),
        help="directory for timestamped backups (default: home directory)",
    )
    delete.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    adopt = session_commands.add_parser(
        "adopt-global",
        parents=[common],
        help="move V1 global sessions of non-git directories into their V2 directory projects",
    )
    adopt.add_argument(
        "--dir",
        action="append",
        default=[],
        metavar="DIR",
        help="only this session directory (repeatable; default: all)",
    )
    adopt.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    adopt.add_argument("--backup", action="store_true", help="write a verified database backup first")
    adopt.add_argument(
        "--backup-dir",
        type=Path,
        default=Path.home(),
        help="directory for timestamped backups (default: home directory)",
    )
    adopt.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    adopt.add_argument(
        "--no-process-check",
        action="store_true",
        help="continue when OpenCode processes cannot be listed (stop them by hand first)",
    )

    tree = session_commands.add_parser(
        "tree",
        parents=[common],
        help="show the session tree around one session, resolved by full id or unique prefix",
    )
    tree.add_argument("session_id", metavar="ID", help="full session id or unique prefix")
    return parser


def _confirm(question: str, args, errors) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        errors.error("error: no terminal to confirm; pass --yes")
        return False
    return input(f"\n{question} [y/N] ").strip().lower() == "y"


def _session_adopt(args, data_dir: Path, console, use_json: bool) -> int:
    artifacts = storage_env.Artifacts(data_dir)
    errors = storage_render.Console(stream=sys.stderr)
    if not artifacts.db_path.is_file():
        errors.error(f"error: database not found: {artifacts.db_path}")
        return 1
    connection = storage_db.connect_readonly(artifacts.db_path)
    try:
        plan = storage_adopt.build_plan(connection, args.dir)
    finally:
        connection.close()
    if not use_json:
        storage_adopt.render_plan(plan, console)
    if args.dry_run or plan["reason"] or not plan["moves"]:
        if use_json:
            print(json.dumps({"dry_run": True, "plan": plan}, indent=2, ensure_ascii=False))
        elif not plan["reason"] and not plan["moves"]:
            console.write("\n  nothing to move")
        if args.dry_run and not use_json:
            console.section("Dry run")
            console.write("  " + console.yellow("nothing was modified"))
        return 1 if plan["reason"] else 0
    if not _confirm(f"Move {plan['sessions']} sessions?", args, errors):
        console.write("aborted, nothing changed")
        return 1
    try:
        result = storage_adopt.execute(
            artifacts, plan, args.backup, args.backup_dir, no_process_check=args.no_process_check
        )
    except (storage_adopt.AdoptError, sqlite3.Error, OSError) as error:
        errors.error(f"error: {error}")
        return 1
    if use_json:
        print(json.dumps({"dry_run": False, "plan": plan, "result": result}, indent=2, ensure_ascii=False))
    else:
        storage_adopt.render_result(plan, result, console)
    return 0


def _session_delete(args, data_dir: Path, console, use_json: bool) -> int:
    artifacts = storage_env.Artifacts(data_dir)
    errors = storage_render.Console(stream=sys.stderr)
    try:
        plan = storage_session.build_plan(artifacts, args.session_id, args.format)
    except storage_session.SessionDeleteError as error:
        errors.error(f"error: {error}")
        return 1
    if args.dry_run:
        if use_json:
            print(json.dumps({"dry_run": True, "plan": plan}, indent=2, ensure_ascii=False))
        else:
            storage_session.render_plan(plan, console)
            console.section("Dry run")
            console.write("  " + console.yellow("nothing was modified"))
        return 0

    if not use_json:
        storage_session.render_plan(plan, console)
    if not _confirm(f"Delete {plan['session']['id']}?", args, errors):
        console.write("aborted, nothing changed")
        return 1
    try:
        result = storage_session.execute(artifacts, plan, args.backup, args.backup_dir)
    except (storage_session.SessionDeleteError, OSError) as error:
        errors.error(f"error: {error}")
        return 1
    if use_json:
        print(json.dumps({"dry_run": False, "plan": plan, "result": result}, indent=2, ensure_ascii=False))
    else:
        storage_session.render_result(plan, result, console)
    return 0


def _session_tree(args, data_dir: Path, console, use_json: bool) -> int:
    """Run the read-only session tree report."""
    artifacts = storage_env.Artifacts(data_dir)
    errors = storage_render.Console(stream=sys.stderr)
    try:
        report = storage_session_tree.build_tree(artifacts, args.session_id)
    except storage_session_tree.SessionTreeError as error:
        errors.error(f"error: {error}")
        return 1
    if use_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        storage_session_tree.render_tree(report, console)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = storage_env.resolve_data_dir(getattr(args, "data_dir", None))
    use_json = getattr(args, "json", False)
    console = storage_render.Console(
        color=False if use_json or getattr(args, "no_color", False) else None
    )

    if args.command == "audit":
        if not data_dir.is_dir():
            storage_render.Console(stream=sys.stderr).error(
                f"data directory not found: {data_dir}"
            )
            return 1
        report = storage_audit.build_report(
            data_dir,
            top=args.top,
            oversized_mib=args.oversized_mib,
            session=args.session,
        )
        if use_json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            storage_audit.render_report(report, console)
        detail = report.get("session_detail")
        if args.session and detail is not None and not detail.get("found"):
            return 1
        return 0

    if args.command == "cleanup":
        artifacts = storage_env.Artifacts(data_dir)
        try:
            result = storage_cleanup.run(
                artifacts,
                clear_events=args.clear_events,
                delete_orphan_diffs=args.delete_orphan_diffs,
                backup_dir=args.backup_dir,
                dry_run=args.dry_run,
                to_backup=args.backup,
                purge_v1=args.purge_v1,
                no_process_check=args.no_process_check,
            )
        except storage_cleanup.CleanupError as error:
            storage_render.Console(stream=sys.stderr).error(f"error: {error}")
            return 1
        if use_json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif result["dry_run"]:
            state = storage_processes.ProcessState(
                checked=result["process_check"]["checked"],
                processes=result["processes"],
                detail=result["process_check"]["detail"],
            )
            use = storage_processes.FileUse(**result["file_use"])
            storage_cleanup.render_plan(
                result["plan"],
                state,
                use,
                result["backup_dir"],
                console,
                backup=result["backup"],
            )
        else:
            storage_cleanup.render_result(result, console)
        return 0

    if args.command == "session" and args.session_command == "delete":
        return _session_delete(args, data_dir, console, use_json)
    if args.command == "session" and args.session_command == "adopt-global":
        return _session_adopt(args, data_dir, console, use_json)
    if args.command == "session" and args.session_command == "tree":
        return _session_tree(args, data_dir, console, use_json)

    return 2


if __name__ == "__main__":
    sys.exit(main())
