"""Snapshot safety and actual startup replay, isolated from the test GUI loop."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location(
    "startup_benchmark",
    Path(__file__).resolve().parents[1] / "tools/benchmark_startup_rendering.py",
)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "Supported snapshot sandbox")
class TestStartupSnapshot(unittest.TestCase):
    def test_hardware_status_does_not_imply_numerical_symbols_active(self):
        item = SimpleNamespace(property=lambda _name: True, isVisible=lambda: True)
        staged = SimpleNamespace(before=1, after=2)
        atlas = SimpleNamespace(count=7, nbytes=256, staged=staged)
        frame = SimpleNamespace(numerical_wind=None, wind=(), raster_wind=None)
        scene = SimpleNamespace(
            frame=frame, weather=item, weather_provider=SimpleNamespace(requests=6)
        )
        stager = SimpleNamespace(band_cache_statistics=lambda: {"current_bytes": 64})
        canvas = SimpleNamespace(
            _gpu=SimpleNamespace(active=True, scene=scene, weather=stager)
        )
        self.assertFalse(benchmark.numerical_weather_status(canvas)["active"])
        frame.numerical_wind = (atlas, 0.5)
        status = benchmark.numerical_weather_status(canvas)
        self.assertTrue(status["active"])
        self.assertEqual(status["symbols"], 7)
        self.assertEqual(status["interval"], [1, 2])
        self.assertEqual(status["band_cache"]["current_bytes"], 64)
        frame.wind = (object(),)
        self.assertFalse(benchmark.numerical_weather_status(canvas)["active"])
        frame.wind = ()
        frame.raster_wind = object()
        self.assertFalse(benchmark.numerical_weather_status(canvas)["active"])
        self.assertFalse(
            benchmark.numerical_weather_status(SimpleNamespace())["enabled"]
        )

    def test_weather_identity_gate_rejects_changed_missing_and_unmatched_content(self):
        from copy import deepcopy

        job = dict(
            layer="wind",
            published=True,
            viewport=dict(latitude=1, longitude=2, zoom=3),
            weather_content=dict(
                surface_sha256="pixels",
                barb_sha256="vectors",
                barb_count=4,
                surface_size=[10, 20],
            ),
        )
        report = dict(runs=[dict(scenario="pan", repeat=0, overlay_jobs=[job])])
        self.assertEqual(
            benchmark.compare_weather_content(report, report),
            dict(
                status="passed",
                matched_frames=1,
                only_baseline_frames=0,
                only_candidate_frames=0,
            ),
        )
        for change, expected in (
            ("hash", "content changed"),
            ("missing", "Missing weather content"),
            ("view", "No matched weather frames"),
        ):
            other = deepcopy(report)
            altered = other["runs"][0]["overlay_jobs"][0]
            if change == "hash":
                altered["weather_content"]["barb_count"] = 0
            elif change == "missing":
                del altered["weather_content"]
            else:
                altered["viewport"]["longitude"] = 5
            with self.assertRaisesRegex(ValueError, expected):
                benchmark.compare_weather_content(report, other)
        self.assertEqual(
            benchmark.compare_weather_content(dict(runs=[]), dict(runs=[]))["status"],
            "not_captured",
        )

    def test_weather_gate_counts_prefetch_promotions_as_candidate_coverage(self):
        from copy import deepcopy

        visible = dict(
            layer="wind",
            published=True,
            request_id=7,
            viewport=dict(latitude=1, longitude=2, zoom=3),
            forecast_time="2026-01-01T00:00:00",
            weather_content=dict(surface_sha256="first"),
        )
        stored = dict(
            layer="wind",
            published=False,
            request_id=8,
            viewport=dict(latitude=1, longitude=4, zoom=3),
            forecast_time="2026-01-01T00:00:00",
            weather_content=dict(surface_sha256="second"),
        )
        report = dict(
            runs=[
                dict(
                    scenario="pan_pause_pan",
                    repeat=0,
                    overlay_jobs=[visible, stored],
                    weather_schedule_events=[
                        dict(
                            event="request",
                            request_id=8,
                            viewport=dict(latitude=1, longitude=4, zoom=3),
                        ),
                        dict(event="publish", request_id=8, elapsed_ms=1),
                    ],
                )
            ]
        )
        result = benchmark.compare_weather_content(report, deepcopy(report))
        self.assertEqual(result["matched_frames"], 2)
        self.assertEqual(result["only_candidate_frames"], 0)
        # Without the promotion event the stored frame is invisible to the gate.
        stranded = deepcopy(report)
        stranded["runs"][0]["weather_schedule_events"] = []
        result = benchmark.compare_weather_content(stranded, stranded)
        self.assertEqual(result["matched_frames"], 1)

    def test_weather_comparison_matches_forecast_time_as_well_as_viewport(self):
        from copy import deepcopy

        job = dict(
            layer="wind",
            published=True,
            viewport=dict(latitude=1, longitude=2, zoom=3),
            forecast_time="2026-01-01T00:00:00",
            weather_content=dict(surface_sha256="first"),
        )
        later = deepcopy(job)
        later["forecast_time"] = "2026-01-01T01:00:00"
        later["weather_content"]["surface_sha256"] = "later"
        report = dict(
            runs=[dict(scenario="scrub_return", repeat=0, overlay_jobs=[job, later])]
        )
        self.assertEqual(
            benchmark.compare_weather_content(report, report)["matched_frames"], 2
        )

    def make_source(self, path):
        path.mkdir()
        (path / "enc").mkdir()
        chart = path / "enc/chart.geojson"
        chart.write_text(
            json.dumps(
                dict(
                    type="FeatureCollection",
                    features=[
                        dict(
                            type="Feature",
                            properties={},
                            geometry=dict(type="Point", coordinates=[-123.4, 48.4]),
                        )
                    ],
                )
            )
        )
        (path / "settings.json").write_text(
            json.dumps(
                dict(
                    chartPalette="dark",
                    canadianCharts=True,
                    gribBarbDensity="dense",
                    gribIsobarInterval=1.0,
                    vectorCharts=[dict(path=str(chart), enabled=True, metadata=None)],
                )
            )
        )
        (path / "conn-manager.json").write_text("private instrument configuration")
        (path / "recordings").mkdir()
        (path / "recordings/private.log").write_text("private history")
        (path / "temp").mkdir()
        (path / "temp/partial.grib2").write_text("incomplete download")

    def test_capture_remaps_paths_excludes_connections_and_preserves_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            self.make_source(source)
            before = benchmark.inventory(source)
            snapshot = root / "snapshot"
            manifest = benchmark.capture(source, snapshot)
            self.assertEqual(benchmark.verify_snapshot(snapshot), manifest)
            self.assertEqual(benchmark.inventory(source), before)
            self.assertFalse((snapshot / "data/conn-manager.json").exists())
            self.assertFalse((snapshot / "data/recordings").exists())
            self.assertFalse((snapshot / "data/temp").exists())
            saved = json.loads((snapshot / "data/settings.json").read_text())
            self.assertEqual(
                saved["vectorCharts"][0]["path"], "@APPDATA@/enc/chart.geojson"
            )
            sandbox = benchmark.prepare_sandbox(snapshot, root / "worker")
            settings = json.loads((sandbox / "settings.json").read_text())
            self.assertEqual(
                settings["vectorCharts"][0]["path"], str(sandbox / "enc/chart.geojson")
            )
            (sandbox / "enc/chart.geojson").write_text("sandbox only")
            self.assertEqual(benchmark.inventory(source), before)
            self.assertEqual(benchmark.verify_snapshot(snapshot), manifest)
            with self.assertRaisesRegex(ValueError, "new directory"):
                benchmark.capture(source, snapshot)
            (snapshot / "data/enc/chart.geojson").write_text("tampered snapshot")
            with self.assertRaisesRegex(ValueError, "snapshot has changed"):
                benchmark.verify_snapshot(snapshot)

    def test_capture_rejects_external_charts_and_symlinked_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            self.make_source(source)
            (source / "settings.json").write_text(
                json.dumps(
                    dict(vectorCharts=[dict(path=str(root / "elsewhere.geojson"))])
                )
            )
            with self.assertRaisesRegex(ValueError, "outside managed storage"):
                benchmark.capture(source, root / "snapshot")
            (source / "settings.json").write_text("{}")
            (source / "enc/link").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlinks"):
                benchmark.capture(source, root / "snapshot")
            self.assertFalse((root / "snapshot").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("PySide6") and sys.platform in ("darwin", "linux"),
        "Requires Qt and supported sandbox",
    )
    def test_real_window_replays_saved_settings_and_leaves_source_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            self.make_source(source)
            before = benchmark.inventory(source)
            snapshot = root / "snapshot"
            benchmark.capture(source, snapshot)
            output = root / "report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/benchmark_startup_rendering.py",
                    "--snapshot",
                    str(snapshot),
                    "--offscreen",
                    "--frames",
                    "20",
                    "--repeats",
                    "1",
                    "--interval-ms",
                    "0",
                    "--scenarios",
                    "stationary,pan,zoom_out",
                    "--output",
                    str(output),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(len(report["runs"]), 3)
            for run in report["runs"]:
                self.assertEqual(run["startup"]["palette"], "dark")
                self.assertEqual(run["startup"]["barb_density"], "dense")
                self.assertEqual(run["startup"]["isobar_interval"], 1.0)
                self.assertTrue(run["startup"]["chs_enabled"])
                self.assertEqual(len(run["samples"]["grab_ms"]), 20)
                self.assertEqual(len(run["samples"]["queued_callback_ms"]), 20)
                self.assertGreater(run["startup_to_worker_idle_ms"], 0)
                self.assertFalse(any(j.get("error") for j in run["overlay_jobs"]))
            self.assertEqual(benchmark.inventory(source), before)
            benchmark.verify_snapshot(snapshot)
            compared = benchmark.protocol.compare_reports(report, report)
            self.assertTrue(
                all(r["p99_change_percent"] == 0 for r in compared["comparisons"])
            )


if __name__ == "__main__":
    unittest.main()
