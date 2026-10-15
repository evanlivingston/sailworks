"""Custom selections must reach the provider requests without inventing data."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from gweatherrouting.core.gribsources.options import DownloadOptions, selection
from gweatherrouting.core.gribsources.noaagfs import NOAAGFSSource
from gweatherrouting.core.gribsources.noaahrrr import NOAAHRRRSource
from gweatherrouting.core.gribsources.eccchrdps import ECCHRDPSource


class DownloadSelectionTests(unittest.TestCase):
    def test_sparse_selected_hours_and_fields_reach_gfs_network(self):
        source = NOAAGFSSource()
        options = DownloadOptions(24, 3, ("pressure",), (0, 9, 24))
        row = source.get_download_list(options)[0]
        config = json.loads(row[4])
        self.assertEqual(config["steps"], [0, 9, 24])
        response = MagicMock()
        response.iter_content.return_value = [b"GRIB-fixture"]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "gweatherrouting.core.gribsources.noaagfs.requests.get",
                return_value=response,
            ) as get,
        ):
            output = source.download(row[4], directory, lambda _: None)
            self.assertTrue(Path(output).is_file())
            self.assertEqual(get.call_count, 3)
            requests = [call.kwargs["params"] for call in get.call_args_list]
            self.assertEqual([r["file"][-3:] for r in requests], ["000", "009", "024"])
            for params in requests:
                self.assertIn("var_PRMSL", params)
                self.assertNotIn("var_UGRD", params)
                self.assertNotIn("lev_10_m_above_ground", params)

    def test_gfs_native_spacing_becomes_three_hourly_after_day_five(self):
        options = DownloadOptions(168, 0)
        config = json.loads(NOAAGFSSource().get_download_list(options)[0][4])
        self.assertIn(1, config["steps"])
        self.assertIn(120, config["steps"])
        self.assertNotIn(121, config["steps"])
        self.assertIn(123, config["steps"])
        self.assertIn(168, config["steps"])

    def test_hrrr_run_caps_and_wind_only_download(self):
        source = NOAAHRRRSource()
        runs = [datetime(2026, 10, 15, h, tzinfo=timezone.utc) for h in (13, 12)]
        with patch.object(source, "_get_available_cycles", return_value=runs):
            rows = source.get_download_list(DownloadOptions(120, 6, ("wind",)))
        configs = [json.loads(row[4]) for row in rows]
        self.assertEqual([c["hours"] for c in configs], [18, 48])
        self.assertEqual(configs[1]["steps"], list(range(0, 49, 6)))
        response = MagicMock()
        response.iter_content.return_value = [b"GRIB-fixture"]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "gweatherrouting.core.gribsources.noaahrrr.requests.get",
                return_value=response,
            ) as get,
        ):
            source.download(rows[0][4], directory, lambda _: None)
            self.assertEqual(get.call_count, 4)
            for call in get.call_args_list:
                self.assertIn("var_VGRD", call.kwargs["params"])
                self.assertNotIn("var_MSLMA", call.kwargs["params"])

    def test_hrdps_pressure_cadence_and_download_plan(self):
        source = ECCHRDPSource()
        with patch.object(source, "_discover", return_value=("20261015", "12")):
            row = source.get_download_list(DownloadOptions(120, 1, ("pressure",)))[0]
        config = json.loads(row[4])
        self.assertEqual(config["steps"], list(range(0, 49, 3)))
        plan = source._download_plan(config)
        self.assertEqual(len(plan), 17)
        self.assertTrue(all("PRMSL_MSL" in url for url in plan))
        config.update(steps=[0, 1, 3], fields=["wind", "pressure"], hours=3)
        plan = source._download_plan(config)
        self.assertEqual(len(plan), 8)
        self.assertEqual(sum("PRMSL" in url for url in plan), 2)

    def test_unsupported_steps_and_fields_rejected(self):
        for config in (
            {"hours": 24, "steps": [25], "fields": ["wind"]},
            {"hours": 24, "steps": [], "fields": ["wind"]},
            {"hours": 24, "steps": [3], "fields": ["waves"]},
            {"hours": 24, "steps": [True], "fields": ["wind"]},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                selection(config, range(25), range(25))

    def test_legacy_gfs_recovery_schedule_is_unchanged(self):
        steps, fields = selection({"hours": 9, "start_hour": 3}, range(25), [3, 6, 9])
        self.assertEqual(steps, [3, 6, 9])
        self.assertEqual(fields, ("wind", "pressure"))

    def test_selected_hours_outside_model_yield_no_misleading_run(self):
        source = NOAAHRRRSource()
        self.assertEqual(
            source.get_download_list(DownloadOptions(120, 3, steps=(120,))), []
        )
