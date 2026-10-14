"""Polar library persistence, safe installation and unchanged routing values."""

import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import weatherrouting

from gweatherrouting.core.polarmanager import PolarManager, json_to_polars
from gweatherrouting.core.polarfiles import (
    install_polar,
    read_polar,
    orc_catalog,
    download_orc_file,
)

BUNDLED = Path(__file__).parents[1] / "gweatherrouting/data/polars/first317.pol"
ORC_DATA = {
    "name": "Test yacht",
    "sailnumber": "123",
    "boat": {"type": "First 31.7"},
    "vpp": {
        "angles": [45, 90, 150],
        "speeds": [4, 8, 12],
        "45": [3, 4, 5],
        "90": [4, 6, 7],
        "150": [3, 5, 6],
    },
}


def response(data):
    result = MagicMock()
    result.__enter__.return_value = result
    result.json.return_value = data
    return result


class TestPolarManager(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.root = self.path / "polars"
        self.root.mkdir()
        for target, value in (
            ("gweatherrouting.core.storage.DATA_DIR", str(self.path)),
            ("gweatherrouting.core.polarmanager.POLAR_DIR", str(self.root)),
        ):
            mock = patch(target, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.manager = PolarManager()

    def test_refresh_deduplicates_and_reloads_only_enabled_files_including_empty(self):
        expected = list(self.manager.polars_files)
        self.assertEqual(len(self.manager.polars), 1)
        for _ in range(3):
            self.manager.refresh_local_polars()
        self.assertEqual(self.manager.polars_files, expected)
        self.assertEqual(len(self.manager.polars), 1)
        for name in list(self.manager.polars):
            self.manager.disable(name)
        reloaded = PolarManager()
        self.assertEqual(reloaded.polars, [])
        self.assertEqual(reloaded.polars_files, expected)
        reloaded.enable("first317.pol")
        self.assertEqual(PolarManager().polars, ["first317.pol"])
        self.assertIsNone(reloaded.get_path("cat20.pol"))

    def test_import_collision_preserves_bytes_and_engine_interpolation(self):
        source = self.path / "custom.pol"
        shutil.copyfile(BUNDLED, source)
        first = self.manager.add_polar_file(source)
        second = self.manager.add_polar_file(source)
        self.assertNotEqual(first, second)
        self.assertEqual(source.read_bytes(), (self.root / first).read_bytes())
        self.assertEqual(source.read_bytes(), (self.root / second).read_bytes())
        original, imported = weatherrouting.Polar(str(source)), read_polar(
            self.root / second
        )
        self.assertEqual(original.tws, imported.tws)
        self.assertEqual(original.twa, imported.twa)
        self.assertEqual(original.speed_table, imported.speed_table)
        for wind in (4, 5.5, 12.7, 60):
            for angle in (0, 45.5, 90, 179):
                self.assertEqual(
                    original.get_speed(wind, math.radians(angle)),
                    imported.get_speed(wind, math.radians(angle)),
                )
        self.manager.add_polar_file(self.root / first)
        self.assertEqual(self.manager.polars.count(first), 1)
        self.assertEqual(self.manager.polars_files.count(first), 1)

    def test_bad_files_rejected_before_installing_and_failed_copy_cleans_staging(self):
        invalid = (
            "bad",
            "TWA\\TWS 4 8\n45 3\n90 4 6\n",
            "TWA\\TWS 8 4\n45 3 4\n90 4 6\n",
            "TWA\\TWS 4 8\n45 nan 4\n90 4 6\n",
            "TWA\\TWS 4 8\n45 -1 4\n90 4 6\n",
        )
        before = sorted(p.name for p in self.root.iterdir())
        path = self.path / "invalid.pol"
        for text in invalid:
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.manager.add_polar_file(path)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        with patch(
            "gweatherrouting.core.polarfiles.shutil.copyfile",
            side_effect=OSError("copy failed"),
        ):
            with self.assertRaises(OSError):
                install_polar(BUNDLED, self.root)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_delete_updates_metadata_and_enabled_state_and_save_failure_restores_file(
        self,
    ):
        name = "first317.pol"
        self.manager.enable(name)
        self.manager.register_polar_file(name, {"name": "Test boat"})
        previous = (self.root / name).read_bytes()
        with patch(
            "gweatherrouting.core.storage.Storage.save",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                self.manager.delete_polar(name)
        self.assertEqual((self.root / name).read_bytes(), previous)
        self.assertTrue(self.manager.is_enabled(name))
        self.assertEqual(self.manager.get_metadata(name)["name"], "Test boat")
        self.manager.delete_polar(name)
        self.assertFalse((self.root / name).exists())
        restored = PolarManager()
        self.assertNotIn(name, restored.polars)
        self.assertNotIn(name, restored.polars_files)
        self.assertNotIn(name, restored.storage.metadata)
        with self.assertRaises(ValueError):
            self.manager.delete_polar("../outside.pol")

    def test_orc_download_uses_shared_conversion_and_persists_metadata(self):
        expected = self.path / "expected.pol"
        json_to_polars(ORC_DATA["vpp"], expected)
        with patch(
            "gweatherrouting.core.polarfiles.requests.get",
            return_value=response(ORC_DATA),
        ) as get:
            name = self.manager.download_orc_polar("USA/123")
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["timeout"], (10, 30))
        self.assertEqual((self.root / name).read_bytes(), expected.read_bytes())
        restored = PolarManager()
        self.assertTrue(restored.is_enabled(name))
        self.assertEqual(restored.get_metadata(name)["name"], "Test yacht")
        self.assertIsNotNone(read_polar(self.root / name))
        with patch(
            "gweatherrouting.core.polarfiles.requests.get",
            side_effect=requests.ConnectionError("offline"),
        ):
            with self.assertRaises(requests.ConnectionError):
                download_orc_file("USA/456", self.root)
        self.assertFalse((self.root / "USA_456.pol").exists())

    def test_catalog_filters_invalid_identifiers_and_network_errors_surface(self):
        entries = [
            ["USA/123", "Test yacht", "First 31.7"],
            ["../outside", "bad", "bad"],
            [None],
            "invalid",
        ]
        with patch(
            "gweatherrouting.core.polarfiles.requests.get",
            return_value=response(entries),
        ):
            self.assertEqual(orc_catalog(), [("USA/123", "Test yacht", "First 31.7")])
        with patch(
            "gweatherrouting.core.polarfiles.requests.get",
            return_value=response({"wrong": "shape"}),
        ):
            with self.assertRaises(ValueError):
                orc_catalog()
        # Storage remains compatible with GTK's existing plain JSON schema.
        payload = json.loads((self.path / "polar-manager.json").read_text())
        self.assertEqual(payload["enabled"], self.manager.polars)

    def test_deleting_a_library_symlink_keeps_its_original_file(self):
        original = self.path / "original.pol"
        shutil.copyfile(BUNDLED, original)
        alias = self.root / "alias.pol"
        alias.symlink_to(original)
        self.manager.refresh_local_polars()
        self.manager.enable(alias.name)
        self.manager.delete_polar(alias.name)
        self.assertFalse(alias.exists())
        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), BUNDLED.read_bytes())
