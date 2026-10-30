"""Tests for Windows path handling, process detection, and the unsafe-check gate."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import storage_backup
import storage_cleanup
import storage_env
import storage_processes
import storage_render


class PathFormTests(unittest.TestCase):
    def test_windows_path_uses_backslashes_for_hashes(self):
        self.assertEqual(storage_env.native_dir("C:/Users/ASUS"), "C:\\Users\\ASUS")
        self.assertEqual(storage_env.native_dir("C:/Users/ASUS/"), "C:\\Users\\ASUS")
        self.assertEqual(storage_env.native_dir("C:\\Users\\ASUS"), "C:\\Users\\ASUS")

    def test_posix_path_is_unchanged(self):
        self.assertEqual(storage_env.native_dir("/home/u/proj/"), "/home/u/proj")
        self.assertEqual(storage_env.native_dir("/"), "/")

    def test_normalize_lowercases_only_windows_paths(self):
        self.assertEqual(storage_env.normalize_dir("C:\\Users\\ASUS\\"), "c:/users/asus")
        self.assertEqual(storage_env.normalize_dir("/home/U/Proj"), "/home/U/Proj")

    def test_same_path_ignores_separator_and_case_on_windows(self):
        self.assertTrue(storage_env.same_path("C:/Users/ASUS", "c:\\users\\asus\\"))
        self.assertFalse(storage_env.same_path("/home/u/proj", "/home/u/other"))

    def test_stored_slash_form_is_normalized(self):
        self.assertTrue(storage_env.is_normalized_dir("C:/Users/ASUS"))
        self.assertTrue(storage_env.is_normalized_dir("C:\\Users\\ASUS"))
        self.assertTrue(storage_env.is_normalized_dir("/home/u/proj"))
        self.assertFalse(storage_env.is_normalized_dir("C:/Users/ASUS/"))
        self.assertFalse(storage_env.is_normalized_dir("/home/u/proj/"))

    def test_unc_path(self):
        self.assertTrue(storage_env.looks_windows("//server/share"))
        self.assertEqual(storage_env.normalize_dir("//server/share"), "//server/share")


class ProjectIdTests(unittest.TestCase):
    def test_windows_id_hashes_the_native_form(self):
        expected = hashlib.sha1(b"directory:C:\\Users\\ASUS").hexdigest()
        self.assertEqual(storage_adopt_project_id("C:/Users/ASUS"), expected)

    def test_candidates_include_both_separators(self):
        candidates = storage_adopt_candidates("C:/Users/ASUS")
        self.assertEqual(len(candidates), 2)
        self.assertIn(hashlib.sha1(b"directory:C:\\Users\\ASUS").hexdigest(), candidates)
        self.assertIn(hashlib.sha1(b"directory:C:/Users/ASUS").hexdigest(), candidates)

    def test_posix_candidates_collapse(self):
        self.assertEqual(len(storage_adopt_candidates("/home/u/proj")), 1)


def storage_adopt_project_id(directory: str) -> str:
    import storage_adopt

    return storage_adopt.project_id(directory)


def storage_adopt_candidates(directory: str) -> list[str]:
    import storage_adopt

    return storage_adopt.project_id_candidates(directory)


class TasklistTests(unittest.TestCase):
    SAMPLE = (
        '"opencode.exe","49508","Console","1","57,412 K"\n'
        '"chrome.exe","1234","Console","1","100,000 K"\n'
        '"OpenChamber.exe","777","Console","1","10,000 K"\n'
        '"opencode-desktop.exe","9","Console","1","1,000 K"\n'
        "\n"
    )

    def test_parses_opencode_images_only(self):
        found = storage_processes.parse_tasklist(self.SAMPLE)
        self.assertEqual([row["pid"] for row in found], ["49508", "777", "9"])

    def test_localized_no_task_message_is_ignored(self):
        self.assertEqual(storage_processes.parse_tasklist("信息: 没有运行的任务匹配指定标准。\n"), [])


class DetectTests(unittest.TestCase):
    def test_windows_backend_uses_the_runner(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(
                returncode=0,
                stdout=b'"opencode.exe","42","Console","1","1 K"\n',
                stderr=b"",
            )

        state = storage_processes.detect(platform="nt", runner=runner)
        self.assertTrue(state.checked)
        self.assertEqual(state.processes, [{"pid": "42", "command": "opencode.exe"}])
        self.assertEqual(calls[0][:2], ["tasklist", "/FO"])

    def test_windows_failure_is_unchecked_not_empty(self):
        def runner(argv, **kwargs):
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"access denied")

        state = storage_processes.detect(platform="nt", runner=runner)
        self.assertFalse(state.checked)
        self.assertEqual(state.processes, [])
        self.assertIn("access denied", state.detail)

    def test_missing_proc_is_unchecked(self):
        state = storage_processes.detect(platform="posix", proc_root=Path("/nonexistent-proc"))
        self.assertFalse(state.checked)
        self.assertIn("not readable", state.detail)

    def test_unknown_platform_is_unchecked(self):
        state = storage_processes.detect(platform="java")
        self.assertFalse(state.checked)
        self.assertIn("java", state.detail)


class ArtifactsTests(unittest.TestCase):
    def test_channel_database_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "opencode-beta.db").write_bytes(b"")
            artifacts = storage_env.Artifacts(Path(directory))
            self.assertEqual(artifacts.db_path.name, "opencode-beta.db")
            self.assertEqual(artifacts.wal_path.name, "opencode-beta.db-wal")

    def test_ambiguous_channel_databases_do_not_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "opencode-beta.db").write_bytes(b"")
            (Path(directory) / "opencode-dev.db").write_bytes(b"")
            artifacts = storage_env.Artifacts(Path(directory))
            self.assertEqual(artifacts.db_path.name, "opencode.db")

    def test_opencode_db_overrides_the_name(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"OPENCODE_DB": "custom.db"}):
                artifacts = storage_env.Artifacts(Path(directory))
                self.assertEqual(artifacts.db_path, Path(directory) / "custom.db")


class GateTests(unittest.TestCase):
    def test_cleanup_refuses_when_the_check_did_not_run(self):
        unchecked = storage_processes.ProcessState(False, [], "no backend")
        with mock.patch.object(storage_processes, "detect", return_value=unchecked):
            with self.assertRaises(storage_cleanup.CleanupError) as caught:
                storage_cleanup.run(
                    storage_env.Artifacts(Path("/nonexistent-opencode")),
                    clear_events=False,
                    delete_orphan_diffs=False,
                    backup_dir=Path(tempfile.gettempdir()),
                )
        self.assertIn("cannot confirm", str(caught.exception))

    def test_escape_hatch_passes_the_gate(self):
        unchecked = storage_processes.ProcessState(False, [], "no backend")
        with mock.patch.object(storage_processes, "detect", return_value=unchecked):
            with self.assertRaises(storage_cleanup.CleanupError) as caught:
                storage_cleanup.run(
                    storage_env.Artifacts(Path("/nonexistent-opencode")),
                    clear_events=False,
                    delete_orphan_diffs=False,
                    backup_dir=Path(tempfile.gettempdir()),
                    no_process_check=True,
                )
        self.assertIn("database not found", str(caught.exception))


class FileUseTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "the /proc walk only exists on POSIX")
    def test_open_file_is_detected_on_posix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            path.write_bytes(b"")
            with path.open("rb"):
                use = storage_processes.file_in_use(path, platform="posix")
            self.assertTrue(use.checked)
            self.assertTrue(use.in_use)
            self.assertIn(str(os.getpid()), use.detail)

    @unittest.skipUnless(os.name == "posix", "the /proc walk only exists on POSIX")
    def test_closed_file_is_not_in_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            path.write_bytes(b"")
            use = storage_processes.file_in_use(path, platform="posix")
            self.assertTrue(use.checked)
            self.assertFalse(use.in_use)

    def test_missing_file_is_not_in_use(self):
        use = storage_processes.file_in_use(
            Path("/nonexistent-opencode/opencode.db"), platform="posix"
        )
        self.assertTrue(use.checked)
        self.assertFalse(use.in_use)

    def test_missing_proc_makes_the_probe_unchecked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            path.write_bytes(b"")
            use = storage_processes.file_in_use(
                path, platform="posix", proc_root=Path("/nonexistent-proc")
            )
            self.assertFalse(use.checked)
            self.assertFalse(use.in_use)

    def test_unknown_platform_is_unchecked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            path.write_bytes(b"")
            use = storage_processes.file_in_use(path, platform="java")
            self.assertFalse(use.checked)


class FileGateTests(unittest.TestCase):
    def _run(self, **kwargs):
        idle = storage_processes.ProcessState(True, [])
        busy = storage_processes.FileUse(True, True, "PID 1 has opencode.db open")
        with mock.patch.object(storage_processes, "detect", return_value=idle):
            with mock.patch.object(storage_processes, "file_in_use", return_value=busy):
                return storage_cleanup.run(
                    storage_env.Artifacts(Path("/nonexistent-opencode")),
                    clear_events=False,
                    delete_orphan_diffs=False,
                    backup_dir=Path(tempfile.gettempdir()),
                    **kwargs,
                )

    def test_an_open_database_blocks_cleanup(self):
        with self.assertRaises(storage_cleanup.CleanupError) as caught:
            self._run()
        self.assertIn("open in another process", str(caught.exception))

    def test_escape_hatch_does_not_bypass_an_open_database(self):
        with self.assertRaises(storage_cleanup.CleanupError) as caught:
            self._run(no_process_check=True)
        self.assertIn("open in another process", str(caught.exception))


class RenderFallbackTests(unittest.TestCase):
    class Narrow(io.StringIO):
        encoding = "cp1252"

    def test_glyphs_fall_back_on_a_narrow_console(self):
        stream = self.Narrow()
        console = storage_render.Console(stream=stream, color=False)
        console.write("  ✓ done · 3 files")
        self.assertIn("ok done - 3 files", stream.getvalue())
        self.assertNotIn("✓", stream.getvalue())

    def test_unicode_console_keeps_glyphs(self):
        class Wide(io.StringIO):
            encoding = "utf-8"

        stream = Wide()
        storage_render.Console(stream=stream, color=False).write("  ✓ done")
        self.assertIn("✓", stream.getvalue())


class ArchiveErrorTests(unittest.TestCase):
    def test_locked_file_is_named_and_others_are_still_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "a.json"
            second = Path(directory) / "b.json"
            first.write_text("{}")
            second.write_text("{}")
            real_unlink = Path.unlink

            def unlink(self, *args, **kwargs):
                if self == first:
                    raise PermissionError(32, "file is in use")
                return real_unlink(self, *args, **kwargs)

            with mock.patch.object(Path, "unlink", unlink):
                with self.assertRaises(storage_backup.BackupError) as caught:
                    storage_backup.archive_and_remove([first, second], None)
            self.assertIn("a.json", str(caught.exception))
            self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
