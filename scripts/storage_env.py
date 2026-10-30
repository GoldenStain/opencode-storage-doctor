"""Locate OpenCode's data directory and artifacts, and compare paths across platforms."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path


DB_NAME = "opencode.db"
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[/\\]|[/\\]{2})")


@dataclass(frozen=True)
class Artifacts:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        """Return the database path, honoring OPENCODE_DB and channel database names."""
        explicit = os.environ.get("OPENCODE_DB")
        if explicit:
            candidate = Path(explicit)
            return candidate if candidate.is_absolute() else self.data_dir / candidate
        default = self.data_dir / DB_NAME
        if default.is_file() or not self.data_dir.is_dir():
            return default
        # Channel builds name the database opencode-<channel>.db. Only a single
        # such file is unambiguous; with several, do not guess.
        others = sorted(
            path for path in self.data_dir.glob("opencode*.db") if path.is_file()
        )
        return others[0] if len(others) == 1 else default

    @property
    def wal_path(self) -> Path:
        return self.db_path.with_name(f"{self.db_path.name}-wal")

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"

    @property
    def diff_dir(self) -> Path:
        return self.storage_dir / "session_diff"

    @property
    def tool_output_dir(self) -> Path:
        return self.data_dir / "tool-output"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshot"

    @property
    def worktree_dir(self) -> Path:
        return self.data_dir / "worktree"


def resolve_data_dir(override: Path | str | None = None) -> Path:
    """Resolve the data directory from an explicit path, environment, or platform default."""
    if override is not None:
        return Path(override).expanduser()
    if os.environ.get("OPENCODE_DATA_DIR"):
        return Path(os.environ["OPENCODE_DATA_DIR"]).expanduser()
    if os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]).expanduser() / "opencode"
    return Path.home() / ".local/share/opencode"


def looks_windows(path: str | Path) -> bool:
    """Return True when a path is written the way Windows writes an absolute path."""
    return bool(_WINDOWS_ABSOLUTE.match(str(path)))


def native_dir(value: str | Path) -> str:
    """Return the form OpenCode hashes for a project id: native separators, no trailing slash.

    Windows paths use backslashes even when read on POSIX, because the id in the
    database was computed by OpenCode on the machine that wrote it.
    """
    text = str(value)
    return ntpath.normpath(text) if looks_windows(text) else posixpath.normpath(text)


def normalize_dir(value: str | Path) -> str:
    """Return a comparison form: one separator style, no trailing slash, lowercased Windows paths."""
    text = str(value)
    if looks_windows(text):
        return ntpath.normpath(text).replace("\\", "/").lower()
    return posixpath.normpath(text)


def same_path(left: str | Path, right: str | Path) -> bool:
    """Compare two paths the way the platform compares them."""
    return normalize_dir(left) == normalize_dir(right)


def is_absolute_dir(value: str | Path) -> bool:
    text = str(value)
    return ntpath.isabs(text) if looks_windows(text) else posixpath.isabs(text)


def is_normalized_dir(value: str | Path) -> bool:
    """Return True when a path has no trailing separator and no redundant segments."""
    text = str(value).replace("\\", "/")
    return text == native_dir(value).replace("\\", "/")


def dir_size(path: Path) -> int:
    """Return the total size in bytes of every regular file under a path."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def usage_entries(data_dir: Path) -> list[dict[str, object]]:
    """Return per-entry usage for each child of the data directory, largest first."""
    entries: list[dict[str, object]] = []
    if not data_dir.is_dir():
        return entries
    for child in data_dir.iterdir():
        try:
            if child.is_dir():
                size = dir_size(child)
            else:
                size = child.stat().st_size
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "bytes": size,
                "is_dir": child.is_dir(),
            }
        )
    entries.sort(key=lambda entry: entry["bytes"], reverse=True)
    return entries
