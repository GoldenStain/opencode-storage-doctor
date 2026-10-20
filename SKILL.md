---
name: opencode-storage-audit
description: Use when OpenCode storage, opencode.db, tool-output, session_diff, or old sessions consume excessive disk space, or when sessions from non-git directories are missing from `opencode session list` after upgrading OpenCode from V1 to V2; audits usage, identifies the largest sessions, safely prepares cleanup plus offline VACUUM, and moves V1 "global" sessions back into the projects V2 lists them under.
---

# OpenCode Storage Audit

Diagnose OpenCode's local storage without reading conversation content, then clean or repair it only with explicit user approval.

## Scripts

One entry point, standard-library Python only:

```bash
python3 scripts/opencode_storage.py audit   [--top N] [--oversized-mib N]
python3 scripts/opencode_storage.py cleanup [--clear-events] [--delete-orphan-diffs] [--purge-v1] [--dry-run]
python3 scripts/opencode_storage.py session delete ID [--format auto|v1|v2] [--dry-run] [--backup] [--yes]
python3 scripts/opencode_storage.py session adopt-global [--dir DIR]... [--dry-run] [--backup] [--yes]
```

Shared options on every subcommand:

- `--data-dir PATH`: override the data directory (defaults to `$XDG_DATA_HOME/opencode`).
- `--no-color`: disable ANSI color; `NO_COLOR` is honored automatically and color is off when output is not a TTY.
- `--json`: emit machine-readable JSON instead of the rendered report.

`audit` is read-only. `cleanup` and `session adopt-global` change the database and refuse to run while OpenCode is active. `session delete` is destructive. Both `session` commands ask for confirmation unless `--yes` is given.

Resolve script paths relative to this skill directory. Do not assume copies exist in the user's home directory.

## Audit Workflow

1. Run the read-only report first:

   ```bash
   python3 scripts/opencode_storage.py audit
   ```

   Optional arguments:

   ```bash
   python3 scripts/opencode_storage.py audit --top 20 --oversized-mib 10
   python3 scripts/opencode_storage.py audit --session ses_1d95e5a37ffe
   ```

   `--session ID` accepts a full session id or any unique prefix and prints a
   focused single-session report (metadata, message/part/event counts and bytes,
   and its largest messages). It exits non-zero when nothing matches or the
   prefix is ambiguous; ambiguous prefixes list the candidate sessions.

2. Report these findings to the user:

   - Total OpenCode data size and per-entry sizes under the data directory.
   - Physical SQLite size, WAL size, freelist bytes, clearable event bytes, V1 residue bytes, and the combined reclaimable percentage.
   - Largest database tables or indexes.
   - Largest sessions ranked by message, part, event, and `session_diff` bytes,
     each cell showing the row count and its byte size. On V2 databases these
     come from `session_v2` and `session_message`, and parts show 0 because V2
     stores them inside messages.
   - Oversized individual messages, including their session IDs and titles.
   - Orphan `session_diff` files.
   - Event-log size, including events whose session no longer exists.
   - V2 databases only: V1 residue, meaning V1 rows and `session_diff` files of
     sessions that V2 has already migrated, and any V1 sessions that V2 lacks.
   - V2 databases only: global sessions that `opencode session list` does not
     show, and how many of them `session adopt-global` can move.
   - Largest `tool-output` files.

3. Explain the difference between logical deletion and physical shrinkage. SQLite `DELETE` adds pages to the freelist; only `VACUUM` returns that space to the filesystem. Reclaimable space is `freelist + event rows + V1 residue rows`. On a database upgraded from V1, `--purge-v1` is usually the largest single win.

4. Never print message, part, event, diff, or tool-output contents. Use metadata and byte lengths only.

## Session Cleanup

- Size alone does not prove a session is useless. Ask the user which sessions may be removed.
- To delete a whole session, use `session delete`. Always show its dry run first and get approval for that exact session id:

  ```bash
  python3 scripts/opencode_storage.py session delete ses_1d95e5 --dry-run
  python3 scripts/opencode_storage.py session delete ses_1d95e5 --yes
  ```

  - The id may be a unique prefix. An ambiguous prefix lists candidates and changes nothing.
  - Child sessions are always deleted with their parent, as `opencode session delete` does.
  - `--format v2` deletes the V2 session through `opencode session delete`. The OpenCode service must be running and must use the default data directory.
  - `--format v1` deletes the session's V1 rows and its `storage/session_diff` file with SQL. It works while OpenCode runs, because only OpenCode's one-time migration reads those tables.
  - `--format auto` (default) runs both steps that apply: V2 first, then V1.
  - The command refuses to delete the session that runs it (`OPENCODE_SESSION_ID`).
  - When the dry run warns that a session "exists only as V1 data", deleting it loses that conversation for good. Say so to the user.
  - Deleted rows only free pages. Run `cleanup` afterwards to VACUUM.
- If one oversized `message.data` row dominates a valuable session, offer a surgical cleanup of `summary.diffs` instead of deleting the session. Back up the database first, preserve the summary object, and replace only its `diffs` array with an empty array.
- If OpenCode reports `session not found`, verify the session ID directly in the database. Child agent sessions may already have been removed or may be hidden from the normal session list.
- Re-run the audit after session deletion. Confirm that no orphan message or part rows remain.

Any direct SQL modification to messages, sessions, parts, or event data is destructive. State the exact rows and estimated bytes, then obtain explicit approval before changing them.

## Global Sessions After the V1 to V2 Upgrade

V1 put every session outside a git repository into one project, `global`. V2 gives each such directory its own project (id = sha1 of `directory:<path>`), but its migration leaves the old sessions in `global`. `opencode session list` lists only the current project, so those sessions seem lost. They are still in the database, and OpenChamber, which lists by directory, still shows them.

`session adopt-global` moves them, directory by directory, into the project V2 uses for that directory. It does what V2 already does for git repositories: it sets `project_id` and an empty `path`, and keeps `time_updated`, so the session order stays the same. It creates the directory project row when it is missing.

1. Show the plan. It is read-only and works while OpenCode runs:

   ```bash
   python3 scripts/opencode_storage.py session adopt-global --dry-run
   ```

   It skips directories that no longer exist, symlinked paths, and directories inside a git or hg tree (V2 adopts those itself when it opens them). Use `--dir DIR` (repeatable) to limit the move.

2. Tell the user to stop every OpenCode process, including the background service (`opencode service stop`) and any app that runs its own `opencode serve`, such as OpenChamber. The command refuses while one runs, because the service keeps session locations in memory.

3. Have the user run, from a normal terminal:

   ```bash
   python3 scripts/opencode_storage.py session adopt-global --backup
   ```

4. After OpenCode restarts, ask the user to run `opencode session list` in a moved directory.

The command refuses when the database is not V2, or when the newest V2 directory project does not follow the known id rule (a different OpenCode version). It also refuses when no directory project exists yet; then the user opens OpenCode once in any non-git directory and retries. The writes happen in one transaction, which rolls back when a moved count differs from the plan. To undo, restore the backup. Until `cleanup --purge-v1` runs, the V1 `session` table also still holds each session's original `project_id` and `path`.

## Tool Output

`tool-output` contains complete output that OpenCode moved out of the UI after truncation. Large files can be legitimate command output or temporary diagnostics.

- Identify files created by the current investigation before deleting them.
- Do not delete unrelated tool output merely because it is large.
- If provenance is uncertain, report the filename and size and ask the user.

## Offline Cleanup

`VACUUM` requires exclusive database access. An agent running inside OpenCode cannot complete this final step while its own `serve` or `attach` process remains active.

1. Decide which optional cleanup flags are approved:

   - `--clear-events`: delete `event` rows, plus `event_sequence` on V1 databases only. Warn that this discards persistent event history and may affect present or future synchronization behavior. On V2 databases `event_sequence` is kept: V2 numbers new session messages from it, and clearing it would break every existing session.
   - `--delete-orphan-diffs`: archive, then remove `session_diff` files with no corresponding live session.
   - `--purge-v1` (V2 databases only): delete the `session`, `message`, `part`, `todo`, and `session_share` rows, and the `session_diff` files, of every V1 session that V2 has migrated. A session counts as migrated when `session_v2` holds it and, if it had V1 messages, `session_message` holds at least one of its messages. Other V1 sessions are kept. The tables stay, empty. The script refuses when V2 has not completed its V1->V2 migration, or when an unknown table has a foreign key to these tables.
     Warn the user before approval:
     - This removes the only lossless copy of V1 history. V2's migration dropped some of it, for example the output of compacted tool calls and per-step metadata.
     - V2 sessions do not change.
     - Restoring needs a database backup from before the purge. Pass `--backup` to write one, together with a zip of the removed diffs.

2. Show a dry run first. It is read-only, needs no exclusive access, and lists exactly which rows and files would be removed with the estimated reclaim:

   ```bash
   python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs --dry-run
   ```

   Omit flags that were not explicitly approved.

3. Tell the user to exit every OpenCode process and run the prepared command from a normal terminal:

   ```bash
   python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs
   ```

   Omit flags that were not explicitly approved.

4. The script must refuse to run while OpenCode is active. With `--backup`, it first creates and verifies a timestamped SQLite backup and zips any diff files it removes; without `--backup` it writes no backup and says so. It then checkpoints WAL, performs `VACUUM`, and runs `integrity_check` afterward. Each step prints as a `✓` line with the before/after sizes and reclaimed percentage. Recommend `--backup` unless the user already holds a recent backup.

5. After OpenCode restarts, run the audit again and ask the user to open several retained sessions.

6. A backup may be deleted after all of the following are true:

   - The cleanup reports `integrity_check=ok`.
   - OpenCode starts normally.
   - Retained sessions open correctly.
   - The user no longer needs deleted history.

Keeping the backup for one or two days is a reasonable precaution.

## Final Report

State:

- Before and after disk usage.
- Reclaimed database, event, V1-residue, orphan-diff, and tool-output bytes separately.
- Sessions removed or surgically modified.
- Backup path and when it can be deleted.
- Any remaining large objects or expected growth sources.
