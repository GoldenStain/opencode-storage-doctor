# AGENTS.md

An OpenCode skill that audits OpenCode's local storage and cleans or repairs it.
`SKILL.md` is the agent-facing contract; `README.md` is the human-facing one.
Keep both in step with the CLI. The `description` in `SKILL.md`'s frontmatter
decides when agents load the skill: name every symptom a subcommand fixes there.
OpenCode takes the skill id from the install directory name and the display name
from `SKILL.md`'s `name`. Keep both equal to the repository name,
`opencode-storage-doctor` (formerly `opencode-storage-audit`).

## Rules

- Standard-library Python 3.8+ only. No third-party packages.
- Run the tests with `python3 -m unittest discover -s tests` before every commit.
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
- `scripts/storage_env.py`: locates the data directory and compares paths across platforms.
- `scripts/storage_processes.py`: reports whether OpenCode is using the database, including when that cannot be determined.
- `scripts/storage_render.py`: renders reports as aligned terminal text.
- `scripts/storage_session.py`: deletes one session tree chosen by id or prefix.
- `scripts/storage_session_tree.py`: reports the session hierarchy around one session.
- `scripts/storage_v1.py`: clears leftover V1 conversation data out of a V2 database.
- `tests/test_windows_paths.py`: tests Windows path forms, process detection, and the unsafe-check gate.

`storage_audit.py` and `storage_env.py` each need an "and". Both are listed
under Known Gaps.

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
   orphans), `resolve_session()`, `match_session()`, `session_candidates()` (prefix
   matching picks the session these commands report or delete).
6. `scripts/storage_processes.py`: `detect()` and `file_in_use()`, their three
   states each, the `tasklist` parsing, the Windows share-mode probe, and the
   `/proc/<pid>/fd` walk. A false "not in use" here is what lets VACUUM into a
   live database.
7. `scripts/storage_env.py`: `resolve_data_dir()`, `Artifacts.db_path` (it
   decides which database every command touches), `normalize_dir()`,
   `native_dir()`, `same_path()`.
8. `scripts/opencode_storage.py`: flag names and defaults (`--backup` off,
   `--format auto`, `--no-process-check` off) and the confirmation logic in
   `_session_delete()`.
9. `SKILL.md`: the approval and warning steps an agent must follow before any
   destructive command.
10. `scripts/storage_adopt.py`: `project_id()`, `project_id_candidates()`,
    `unavailable_reason()` (the id-rule check), the skip rules in
    `_skip_reason()` and `build_plan()`, `apply()`, and the process gate in
    `execute()`. Not the `render_*` functions or `summary()` (L2).
11. `scripts/storage_session_tree.py`: `_load()`, `_ancestors()`, `_children_map()`,
   `_descendants()`, `_message_stats()` (a cycle or a wrong precedence rule silently
   prints the wrong tree). Not the `render_*` functions (L2).

L1: `scripts/storage_audit.py` (report contents), the estimates in `storage_v1.py`.
L2: `scripts/storage_render.py`, the `render_*` functions, `README.md`.

## Known Gaps

1. **Destructive paths are still untested.** `tests/test_windows_paths.py`
   covers path forms, process detection, the unsafe-check gate, and archive
   errors. `_purge`, `cleanup.run` end to end, and `session delete` still need
   fixture databases and assertions, for V1 and V2 layouts.
2. **Command modules mix deletion logic with rendering.** `storage_cleanup.py`,
   `storage_session.py`, and `storage_adopt.py` hold L0 logic and L2 `render_*`
   functions in one file; `storage_audit.py` (about 530 logic lines) both builds
   and renders; `storage_session_tree.py` holds the relationship walk and its
   rendering. Move rendering out so L0 files hold only L0 code.
3. **`storage_env.py` holds two concepts.** Path resolution and usage
   measurement change for different reasons.
4. **The destructive gate is check-then-act.** `file_in_use()` catches any process
   holding the database open, whatever its name, but it runs seconds before
   VACUUM: a process that starts in between makes VACUUM fail with "database is
   locked", not corrupt the file. The process list still matches by name, so a
   renamed binary shows up only as "another process". Closing the window needs
   holding SQLite's own lock across the whole operation.
5. **The project-id rule is version-dependent.** `project_id_candidates()`
   accepts the native and slash forms because one OpenCode migration rewrote
   stored paths without recomputing ids. It was verified against one Linux and
   one Windows build; a release that changes the hash input again would be
   refused as "unsupported OpenCode version", not silently mis-adopted.
