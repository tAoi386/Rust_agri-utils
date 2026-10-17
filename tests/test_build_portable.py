"""Portable updates preserve projects and leave the last usable build intact."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.build_portable import promote_portable

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PortablePromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".portable-test-", dir=PROJECT_ROOT)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(PROJECT_ROOT))
        self.assertNotEqual(self.root, PROJECT_ROOT)
        (self.root / "README.md").write_text("new documentation", encoding="utf-8")
        (self.root / "examples").mkdir()
        self.stage = self.root / "dist" / "build-new" / "RustSeedLab"
        (self.stage / "_internal").mkdir(parents=True)
        (self.stage / "RustSeedLab.exe").write_bytes(b"new executable")
        self.destination = self.root / "dist" / "capture" / "RustSeedLab"
        self.running = patch("tools.build_portable.assert_not_running")
        self.running_check = self.running.start()

    def tearDown(self):
        self.running.stop()
        self.assertTrue(self.root.is_relative_to(PROJECT_ROOT))
        self.assertNotEqual(self.root, PROJECT_ROOT)
        self.temp.cleanup()

    def old_portable(self, folder):
        (folder / "data").mkdir(parents=True)
        (folder / "RustSeedLab.exe").write_bytes(b"old executable")
        (folder / "data" / "autosave.json").write_text('{"stock": 42}', encoding="utf-8")
        (folder / "蓝浆果2.json").write_text('{"crop": "blue"}', encoding="utf-8")

    def test_existing_projects_and_autosave_survive_with_recoverable_previous_folder(self):
        self.old_portable(self.destination)
        backup = promote_portable(self.stage, self.destination, root=self.root)
        self.assertEqual((self.destination / "RustSeedLab.exe").read_bytes(), b"new executable")
        self.assertEqual((backup / "RustSeedLab.exe").read_bytes(), b"old executable")
        for name in ("data/autosave.json", "蓝浆果2.json"):
            self.assertEqual((self.destination / name).read_bytes(), (backup / name).read_bytes())
        self.assertEqual((self.destination / "README.md").read_text(encoding="utf-8"), "new documentation")

    def test_first_capture_build_migrates_projects_without_changing_previous_version(self):
        previous = self.root / "dist" / "beginner" / "RustSeedLab"
        self.old_portable(previous)
        backup = promote_portable(self.stage, self.destination, root=self.root, migration=previous)
        self.assertIsNone(backup)
        self.assertEqual((previous / "RustSeedLab.exe").read_bytes(), b"old executable")
        self.assertEqual((self.destination / "蓝浆果2.json").read_bytes(), (previous / "蓝浆果2.json").read_bytes())

    def test_running_current_program_keeps_both_current_and_staged_builds_untouched(self):
        self.old_portable(self.destination)
        self.running_check.side_effect = RuntimeError("still running")
        with self.assertRaisesRegex(RuntimeError, "still running"):
            promote_portable(self.stage, self.destination, root=self.root)
        self.assertEqual((self.destination / "RustSeedLab.exe").read_bytes(), b"old executable")
        self.assertEqual((self.stage / "RustSeedLab.exe").read_bytes(), b"new executable")

    def test_failed_promotion_restores_the_previous_portable_folder(self):
        self.old_portable(self.destination)
        original_rename = Path.rename

        def rename(path, destination):
            if path == self.stage:
                raise OSError("simulated failed promotion")
            return original_rename(path, destination)

        with patch.object(Path, "rename", rename):
            with self.assertRaisesRegex(OSError, "simulated failed promotion"):
                promote_portable(self.stage, self.destination, root=self.root)
        self.assertEqual((self.destination / "RustSeedLab.exe").read_bytes(), b"old executable")
        self.assertEqual((self.stage / "RustSeedLab.exe").read_bytes(), b"new executable")

    def test_destination_outside_workspace_is_rejected_before_any_change(self):
        with self.assertRaises(ValueError):
            promote_portable(self.stage, self.root.parent / "outside-workspace", root=self.root)
        self.assertTrue((self.stage / "RustSeedLab.exe").is_file())


if __name__ == "__main__":
    unittest.main()
