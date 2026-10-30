"""Report whether OpenCode is using the database, from process names and open files."""

from __future__ import annotations

import csv
import io
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


TIMEOUT_SECONDS = 15
WINDOWS_IMAGE_NAMES = frozenset(
    {"opencode.exe", "opencode-desktop.exe", "opencode-cli.exe", "openchamber.exe"}
)
WINDOWS_IMAGE_PREFIXES = ("opencode", "openchamber")
POSIX_COMMAND_NAMES = frozenset({"opencode", "opencode-desktop", "openchamber"})


@dataclass
class ProcessState:
    """The result of a process check: what was found, and whether the check could run.

    `checked` is False when the platform has no detection backend or the check
    failed. Callers must treat an unchecked state as "cannot confirm stopped" and
    refuse to run, never as "no processes".
    """

    checked: bool
    processes: list[dict[str, str]] = field(default_factory=list)
    detail: str = ""


def parse_tasklist(text: str) -> list[dict[str, str]]:
    """Return OpenCode entries from `tasklist /FO CSV /NH` output."""
    processes: list[dict[str, str]] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        name = row[0].strip()
        pid = row[1].strip()
        lowered = name.lower()
        if lowered in WINDOWS_IMAGE_NAMES or lowered.startswith(WINDOWS_IMAGE_PREFIXES):
            processes.append({"pid": pid, "command": name})
    return processes


def _windows(runner) -> list[dict[str, str]]:
    result = runner(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").strip()
        raise RuntimeError(detail.decode(errors="replace") or f"tasklist exited {result.returncode}")
    output = result.stdout or b""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return parse_tasklist(output)


def _posix(proc_root: Path) -> list[dict[str, str]]:
    if not proc_root.is_dir():
        raise RuntimeError(f"{proc_root} is not readable; only Linux exposes process details there")
    processes: list[dict[str, str]] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() not in POSIX_COMMAND_NAMES:
                continue
            cmdline = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
                .strip()
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        processes.append({"pid": entry.name, "command": cmdline or "opencode"})
    return processes


def detect(platform: str | None = None, proc_root: Path | None = None, runner=None) -> ProcessState:
    """Return the OpenCode processes that are running, or why the check could not run."""
    name = platform or os.name
    runner = runner or subprocess.run
    try:
        if name == "nt":
            return ProcessState(True, _windows(runner))
        if name == "posix":
            return ProcessState(True, _posix(proc_root or Path("/proc")))
        return ProcessState(False, [], f"no process detector for platform {name!r}")
    except Exception as error:  # a failed check must report, never crash the caller
        return ProcessState(False, [], str(error))


@dataclass
class FileUse:
    """Whether another process holds a file open, and whether that could be checked.

    This does not depend on process names, so it catches a renamed binary or a
    runtime such as `bun.exe` that the name list cannot match.
    """

    in_use: bool
    checked: bool
    detail: str = ""


def _windows_in_use(path: Path) -> FileUse:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = wintypes.BOOL

    generic_read_write = 0x80000000 | 0x40000000
    open_existing = 3
    file_attribute_normal = 0x80
    error_sharing_violation = 32
    error_access_denied = 5
    invalid_handle = ctypes.c_void_p(-1).value

    for attempt in range(2):
        handle = create_file(
            str(path), generic_read_write, 0, None, open_existing, file_attribute_normal, None
        )
        if handle != invalid_handle:
            close_handle(handle)
            return FileUse(False, True)
        error = ctypes.get_last_error()
        if error == error_sharing_violation:
            if attempt == 0:
                # A scanner or the indexer can hold a file for a moment.
                time.sleep(0.15)
                continue
            return FileUse(True, True, "another process holds it open")
        if error == error_access_denied:
            return FileUse(False, False, "access denied while opening the database")
        return FileUse(False, False, f"CreateFile failed with error {error}")
    return FileUse(False, False, "unreachable")


def _posix_in_use(path: Path, proc_root: Path) -> FileUse:
    if not proc_root.is_dir():
        raise RuntimeError(f"{proc_root} is not readable; cannot inspect open file handles")
    target = os.path.realpath(path)
    names = {target, f"{target}-wal", f"{target}-shm"}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            handles = list((entry / "fd").iterdir())
        except OSError:
            # Another user's process, or it exited mid-walk. Only this user can
            # open this database, so a skipped entry cannot be the writer.
            continue
        for handle in handles:
            try:
                link = os.readlink(handle)
            except OSError:
                continue
            if link in names:
                return FileUse(True, True, f"PID {entry.name} has {Path(link).name} open")
    return FileUse(False, True)


def file_in_use(path: Path, platform: str | None = None, proc_root: Path | None = None) -> FileUse:
    """Return whether another process holds the database open."""
    name = platform or os.name
    if not path.exists():
        return FileUse(False, True, "database file does not exist")
    try:
        if name == "nt":
            return _windows_in_use(path)
        if name == "posix":
            return _posix_in_use(path, proc_root or Path("/proc"))
        return FileUse(False, False, f"no file-usage probe for platform {name!r}")
    except Exception as error:  # an unavailable probe must report, not crash
        return FileUse(False, False, str(error))
