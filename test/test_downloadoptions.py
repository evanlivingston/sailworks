"""Custom selections must reach the provider requests without inventing data."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import patch, MagicMock

from gweatherrouting.core.gribsources.options import DownloadOptions, selection
from gweatherrouting.core.gribsources.noaagfs import NOAAGFSSource
from gweatherrouting.core.gribsources.noaahrrr import NOAAHRRRSource
from gweatherrouting.core.gribsources.eccchrdps import ECCHRDPSource
from gweatherrouting.core.gribsources.parallel import write_ordered_forecasts


def response_with(content=b"GRIB-fixture"):
    response = MagicMock()
    response.iter_content.return_value = [content]
    return response


class DownloadSelectionTests(unittest.TestCase):
    def test_gust_is_opt_in_and_downloaded_by_gfs(self):
        self.assertEqual(DownloadOptions().fields, ("wind", "pressure"))
        source = NOAAGFSSource()
        options = DownloadOptions(6, 3, ("gust",), (0, 3, 6))
        row = source.get_download_list(options)[0]
        config = json.loads(row[4])
        self.assertEqual(config["fields"], ["gust"])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "gweatherrouting.core.gribsources.noaagfs.requests.get",
                side_effect=lambda *_args, **_kwargs: response_with(),
            ) as get,
        ):
            source.download(row[4], directory, lambda _: None)
            self.assertEqual(get.call_count, 3)
            for call in get.call_args_list:
                params = call.kwargs["params"]
                self.assertEqual(params["lev_surface"], "on")
                self.assertEqual(params["var_GUST"], "on")
                self.assertNotIn("var_UGRD", params)
                self.assertNotIn("var_VGRD", params)
                self.assertNotIn("var_PRMSL", params)

    def test_gust_is_downloaded_by_hrrr_when_selected(self):
        source = NOAAHRRRSource()
        run = datetime(2026, 10, 15, 3, tzinfo=timezone.utc)
        with patch.object(source, "_get_available_cycles", return_value=[run]):
            row = source.get_download_list(DownloadOptions(3, 3, ("gust",), (0, 3)))[0]
        config = json.loads(row[4])
        self.assertEqual(config["fields"], ["gust"])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "gweatherrouting.core.gribsources.noaahrrr.requests.get",
                side_effect=lambda *_args, **_kwargs: response_with(),
            ) as get,
        ):
            source.download(row[4], directory, lambda _: None)
            self.assertEqual(get.call_count, 2)
            for call in get.call_args_list:
                params = call.kwargs["params"]
                self.assertEqual(params["lev_surface"], "on")
                self.assertEqual(params["var_GUST"], "on")
                self.assertNotIn("var_UGRD", params)
                self.assertNotIn("var_VGRD", params)
                self.assertNotIn("var_MSLMA", params)

    def test_unsupported_hrdps_gust_selection_has_no_download_rows(self):
        source = ECCHRDPSource()
        with patch.object(source, "_discover", return_value=("20261015", "12")):
            self.assertEqual(
                source.get_download_list(DownloadOptions(24, 3, ("gust",))), []
            )

    def test_sparse_selected_hours_and_fields_reach_gfs_network(self):
        source = NOAAGFSSource()
        options = DownloadOptions(24, 3, ("pressure",), (0, 9, 24))
        row = source.get_download_list(options)[0]
        config = json.loads(row[4])
        self.assertEqual(config["steps"], [0, 9, 24])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "gweatherrouting.core.gribsources.noaagfs.requests.get",
                side_effect=lambda *_args, **kwargs: response_with(
                    kwargs["params"]["file"][-3:].encode()
                ),
            ) as get,
        ):
            output = source.download(row[4], directory, lambda _: None)
            self.assertTrue(Path(output).is_file())
            self.assertEqual(get.call_count, 3)
            requests = [call.kwargs["params"] for call in get.call_args_list]
            self.assertEqual({r["file"][-3:] for r in requests}, {"000", "009", "024"})
            self.assertEqual(Path(output).read_bytes(), b"000009024")
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
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "gweatherrouting.core.gribsources.noaahrrr.requests.get",
                side_effect=lambda *_args, **_kwargs: response_with(),
            ) as get,
        ):
            source.download(rows[0][4], directory, lambda _: None)
            self.assertEqual(get.call_count, 4)
            for call in get.call_args_list:
                self.assertIn("var_VGRD", call.kwargs["params"])
                self.assertNotIn("var_MSLMA", call.kwargs["params"])

    def test_parallel_forecasts_are_bounded_and_written_in_step_order(self):
        barrier = Barrier(4)
        second_step_done = Event()
        active_lock = Lock()
        active = 0
        peak_active = 0

        class Response:
            def __init__(self, step):
                self.step = step

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                if self.step == 0:
                    self.assert_second_step_completed()
                yield bytes((self.step,))

            @staticmethod
            def assert_second_step_completed():
                if not second_step_done.wait(timeout=3):
                    raise AssertionError("forecast requests did not run concurrently")

            def close(self):
                nonlocal active
                with active_lock:
                    active -= 1
                if self.step == 1:
                    second_step_done.set()

        def request(step):
            nonlocal active, peak_active
            with active_lock:
                active += 1
                peak_active = max(peak_active, active)
            if step < 4:
                barrier.wait(timeout=3)
            return Response(step)

        output = BytesIO()
        progress = []
        written = write_ordered_forecasts(
            output,
            tuple(range(5)),
            request,
            lambda: None,
            progress.append,
        )

        self.assertEqual(peak_active, 4)
        self.assertEqual(written, 5)
        self.assertEqual(output.getvalue(), bytes(range(5)))
        self.assertEqual(progress, [20, 40, 60, 80, 100])

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
