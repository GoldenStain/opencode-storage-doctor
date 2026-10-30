"""Write verified backups before a destructive change."""

from __future__ import annotations

import datetime
import sqlite3
import zipfile
from pathlib import Path

import storage_db


class BackupError(Exception):
    """A backup could not be written or did not verify."""


def timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_database(db_path: Path, backup: Path) -> None:
    """Copy the database with the SQLite backup API, then integrity-check the copy."""
    source = storage_db.connect_readonly(db_path)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination, pages=4096)
        check = destination.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        destination.close()
        source.close()
    if check != "ok":
        backup.unlink(missing_ok=True)
        raise BackupError(f"backup integrity_check failed: {check}")


def archive_and_remove(paths: list[Path], archive: Path | None) -> None:
    """Zip and verify the files when archive is given, then delete them.

    Deletion is per file so one locked file does not stop the others, and the
    error names what is left. Windows refuses to delete a file another process
    holds open.
    """
    if archive is not None:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in paths:
                output.write(path, arcname=path.name)
        with zipfile.ZipFile(archive) as verify:
            if verify.testzip() is not None:
                raise BackupError(f"diff archive failed verification: {archive}")
            if len(verify.namelist()) != len(paths):
                raise BackupError(f"diff archive count mismatch: {archive}")
    removed = 0
    failed: list[str] = []
    for path in paths:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as error:
            failed.append(f"{path} ({error.strerror or error})")
    if failed:
        raise BackupError(
            f"deleted {removed} of {len(paths)} files; {len(failed)} were kept:\n  "
            + "\n  ".join(failed[:10])
            + ("\n  ..." if len(failed) > 10 else "")
        )
