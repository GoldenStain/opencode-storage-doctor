# AGENTS.md

An OpenCode skill that audits OpenCode's local storage and cleans or repairs it.
`SKILL.md` is the agent-facing contract; `README.md` is the human-facing one.
Keep both in step with the CLI. The `description` in `SKILL.md`'s frontmatter
decides when agents load the skill: name every symptom a subcommand fixes there.

## Rules

- Standard-library Python 3.8+ only. No third-party packages.
- Never read or print message, part, event, diff, or tool-output contents. Use
  ids, counts, and byte lengths only.
- Every destructive path has a read-only dry run that lists exactly what it
  would remove. Keep the two in step.
- Test destructive changes on a copy of a real database (SQLite backup API into
  `/tmp/opencode/...`), never on `~/.local/share/opencode/opencode.db`.
  `--data-dir` points every subcommand at the copy, except
  `session delete --format v2`, which goes through the OpenCode service and
  must be tested on a throwaway session.
- Facts about OpenCode V2 internals (table roles, `event_sequence`, the
  `migration.v1-v2` kv key, the directory project id `sha1("directory:" + path)`)
  were read from the V2 binary. Re-check them against a new OpenCode release
  before relying on them.

## Repo Shape

- `scripts/opencode_storage.py`: command-line entry point that parses arguments for each subcommand.
- `scripts/storage_adopt.py`: reassigns V1 global sessions to the V2 project of their directory.
- `scripts/storage_audit.py`: builds and renders the read-only audit report.
- `scripts/storage_backup.py`: writes verified backups before a destructive change.
- `scripts/storage_cleanup.py`: runs the offline cleanup that ends in VACUUM.
- `scripts/storage_db.py`: runs small read-only queries against the database.
- `scripts/storage_env.py`: locates the data directory and detects running OpenCode processes.
- `scripts/storage_render.py`: renders reports as aligned terminal text.
- `scripts/storage_session.py`: deletes one session tree chosen by id or prefix.
- `scripts/storage_v1.py`: clears leftover V1 conversation data out of a V2 database.

Two entries need an "and" (`storage_audit.py`, `storage_env.py`). Both are
listed under Known Gaps.

## Review Tiers

L0 (read line by line):

1. `scripts/storage_v1.py`: `TABLES` order, `MIGRATED_SQL`, `IDS_SQL`,
   `unavailable_reason()`, `_purge()`. Not `_measure()`/`residue()`, which only estimate (L1).
2. `scripts/storage_session.py`: `build_plan()` (`_subtree()`, step choice,
   refusals), `delete_v2()` guards, `delete_v1()`. Not the `render_*` functions (L2).
3. `scripts/storage_cleanup.py`: `run()`, in particular the `event_sequence`
   condition, the orphan-diff selection, and the quick_check/VACUUM/integrity_check
   order. Not the `render_*` functions (L2).
4. `scripts/storage_backup.py`: `backup_database()` verification and
   `archive_and_remove()`, which deletes files after the zip verifies.
5. `scripts/storage_db.py`: `is_v2_layout()`, `session_tables()`,
   `live_session_ids()` (they decide which sessions are live, so which diffs are
   orphans), and `resolve_session()` (prefix matching picks the session to delete).
6. `scripts/storage_env.py`: `running_processes()` (the gate before VACUUM) and
   `resolve_data_dir()`.
7. `scripts/opencode_storage.py`: flag names and defaults (`--backup` off,
   `--format auto`) and the confirmation logic in `_session_delete()`.
8. `SKILL.md`: the approval and warning steps an agent must follow before any
   destructive command.
9. `scripts/storage_adopt.py`: `project_id()`, `unavailable_reason()` (the id-rule
   check), the skip rules in `_skip_reason()` and `build_plan()`, `apply()`, and the
   process gate in `execute()`. Not the `render_*` functions or `summary()` (L2).

L1: `scripts/storage_audit.py` (report contents), the estimates in `storage_v1.py`.
L2: `scripts/storage_render.py`, the `render_*` functions, `README.md`.

## Known Gaps

1. **No automated tests.** Every check so far was an ad-hoc script against a
   database copy. Destructive paths (`_purge`, `cleanup.run`, `session delete`)
   need fixture databases and assertions, for V1 and V2 layouts.
2. **Command modules mix deletion logic with rendering.** `storage_cleanup.py`,
   `storage_session.py`, and `storage_adopt.py` hold L0 logic and L2 `render_*`
   functions in one file; `storage_audit.py` (about 530 logic lines) both builds
   and renders. Move rendering out so L0 files hold only L0 code.
3. **`storage_env.py` holds two concepts.** Path resolution and process
   detection change for different reasons.
4. **Process detection matches by name.** `running_processes()` looks for
   processes named `opencode`. It misses other writers, such as OpenChamber
   Desktop or a renamed binary. Checking which processes hold
   `opencode.db` open (`/proc/<pid>/fd`) is more accurate.
