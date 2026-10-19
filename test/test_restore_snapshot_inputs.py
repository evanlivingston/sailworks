"""Exact fixture recovery must never overwrite existing or live user inputs."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import restore_snapshot_inputs as recovery


class TestSnapshotRestore(unittest.TestCase):
    def test_explicit_partial_restore_remains_incomplete_without_manifest_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot, source, manifest = self.fixture(root)
            source_extra = source / "unavailable.json"
            source_extra.write_bytes(b"original-metadata")
            entries = recovery.inventory(source)
            manifest.update(
                files=entries, content_sha256=recovery.digest_inventory(entries)
            )
            (snapshot / "manifest.json").write_text(json.dumps(manifest))
            source_extra.unlink()
            original = (snapshot / "manifest.json").read_bytes()
            result = recovery.restore(
                snapshot, [source], apply=True, allow_partial=True
            )
            self.assertTrue(result["applied"])
            self.assertFalse(result["complete"])
            self.assertEqual(result["unrecoverable"], ["unavailable.json"])
            self.assertTrue((snapshot / "data/grib/field.grib2").exists())
            self.assertEqual((snapshot / "manifest.json").read_bytes(), original)
            self.assertNotEqual(result["restored_sha256"], manifest["content_sha256"])

    def fixture(self, root):
        snapshot, source = root / "snapshot", root / "live"
        (snapshot / "data").mkdir(parents=True)
        (source / "grib").mkdir(parents=True)
        (source / "settings.json").write_bytes(b"original-config")
        (source / "grib/field.grib2").write_bytes(b"original-forecast")
        entries = recovery.inventory(source)
        manifest = dict(
            snapshot_version=1,
            files=entries,
            content_sha256=recovery.digest_inventory(entries),
        )
        (snapshot / "manifest.json").write_text(json.dumps(manifest))
        (snapshot / "data/settings.json").write_bytes(b"original-config")
        return snapshot, source, manifest

    def test_plan_then_restore_exactly_without_hardlink_or_manifest_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, source, manifest = self.fixture(Path(temporary))
            original = (snapshot / "manifest.json").read_bytes()
            plan = recovery.restore(snapshot, [source])
            self.assertEqual(plan["recoverable_files"], 1)
            self.assertFalse((snapshot / "data/grib/field.grib2").exists())
            result = recovery.restore(snapshot, [source], apply=True)
            self.assertTrue(result["applied"])
            self.assertEqual(result["restored_sha256"], manifest["content_sha256"])
            self.assertEqual((snapshot / "manifest.json").read_bytes(), original)
            self.assertNotEqual(
                (source / "grib/field.grib2").stat().st_ino,
                (snapshot / "data/grib/field.grib2").stat().st_ino,
            )
            self.assertEqual(
                (source / "grib/field.grib2").read_bytes(), b"original-forecast"
            )

    def test_existing_changed_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, source, _ = self.fixture(Path(temporary))
            (snapshot / "data/settings.json").write_bytes(b"user-change")
            with self.assertRaisesRegex(ValueError, "not overwriting"):
                recovery.restore(snapshot, [source], apply=True)
            self.assertEqual(
                (snapshot / "data/settings.json").read_bytes(), b"user-change"
            )
            self.assertFalse((snapshot / "data/grib/field.grib2").exists())

    def test_wrong_source_is_not_accepted_or_copied(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, source, _ = self.fixture(Path(temporary))
            (source / "grib/field.grib2").write_bytes(b"different-forecast")
            result = recovery.restore(snapshot, [source], apply=True)
            self.assertEqual(result["unrecoverable"], ["grib/field.grib2"])
            self.assertFalse(result["applied"])
            self.assertFalse((snapshot / "data/grib/field.grib2").exists())

    def test_unsafe_paths_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("../escape", "/absolute", "a/../escape", "a//b", "."):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    recovery.safe_path(root, name)
            (root / "link").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "Symlink"):
                recovery.safe_path(root, "link/input")


if __name__ == "__main__":
    unittest.main()
