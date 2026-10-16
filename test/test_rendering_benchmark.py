"""Benchmark protocol checks; performance thresholds are not portable CI tests."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "benchmark_rendering",
    Path(__file__).resolve().parents[1] / "tools/benchmark_rendering.py",
)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)
DEFAULT_SCENE = benchmark.DEFAULT_SCENE
compare_reports = benchmark.compare_reports
file_hash = benchmark.file_hash
read_scene = benchmark.read_scene
scene_identity = benchmark.scene_identity
summary = benchmark.summary


class TestRenderingBenchmark(unittest.TestCase):
    def test_percentiles_and_stall_counts_do_not_hide_rare_slow_frames(self):
        stats = summary([1.0] * 99 + [200.0])
        self.assertEqual(stats["p50_ms"], 1)
        self.assertEqual(stats["p95_ms"], 1)
        self.assertAlmostEqual(stats["p99_ms"], 2.99)
        self.assertEqual(stats["max_ms"], 200)
        self.assertEqual(stats["over_50_ms"], 1)
        self.assertEqual(summary([]), {"count": 0})

    def test_manifest_resolves_and_checks_local_dataset_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            dataset = path / "chart.geojson"
            dataset.write_text("fixture")
            manifest = path / "scene.json"
            manifest.write_text(
                json.dumps(
                    dict(
                        datasets=[
                            dict(
                                kind="vector",
                                path=dataset.name,
                                sha256=file_hash(dataset),
                            )
                        ]
                    )
                )
            )
            scene = read_scene(manifest)
            self.assertEqual(scene["datasets"][0]["path"], str(dataset.resolve()))
            self.assertNotIn("path", scene_identity(scene)["datasets"][0])
            dataset.write_text("changed")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                read_scene(manifest)

    def test_manifest_rejects_unknown_settings_and_invalid_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.json"
            for content in ({"unexpected": 1}, {"wind_side": 0}, {"zoom": 30}):
                path.write_text(json.dumps(content))
                with self.assertRaises(ValueError):
                    read_scene(path)
        self.assertEqual(read_scene(), DEFAULT_SCENE)

    @unittest.skipUnless(
        importlib.util.find_spec("PySide6") and sys.platform != "win32",
        "Requires Qt and the macOS/Linux sandbox",
    )
    def test_isolated_replay_and_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            # A deliberately unusable parent data directory must never be read.
            decoy = path / "parent-data"
            decoy.mkdir()
            sentinels = [
                decoy / "Library/Application Support/gweatherrouting/settings.json",
                decoy / ".gweatherrouting/settings.json",
            ]
            for sentinel in sentinels:
                sentinel.parent.mkdir(parents=True)
                sentinel.write_text("do not touch")
            parent_files = sorted(str(p) for p in decoy.rglob("*"))
            manifest = path / "scene.json"
            chart = path / "chart.geojson"
            chart.write_text(
                json.dumps(
                    dict(
                        type="FeatureCollection",
                        features=[
                            dict(
                                type="Feature",
                                properties={},
                                geometry=dict(
                                    type="Polygon",
                                    coordinates=[
                                        [
                                            [-124, 48],
                                            [-123, 48],
                                            [-123, 49],
                                            [-124, 49],
                                            [-124, 48],
                                        ]
                                    ],
                                ),
                            )
                        ],
                    )
                )
            )
            manifest.write_text(
                json.dumps(
                    dict(
                        navigation_points=100,
                        log_points=100,
                        wind_side=10,
                        datasets=[
                            dict(
                                kind="vector", path=chart.name, sha256=file_hash(chart)
                            )
                        ],
                    )
                )
            )
            output = path / "baseline.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/benchmark_rendering.py",
                    "--offscreen",
                    "--manifest",
                    str(manifest),
                    "--frames",
                    "20",
                    "--repeats",
                    "1",
                    "--scenarios",
                    "empty_pan,combined_pan,zoom_out,reverse_pan",
                    "--output",
                    str(output),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=dict(os.environ, XDG_DATA_HOME=str(decoy), HOME=str(decoy)),
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for sentinel in sentinels:
                self.assertEqual(sentinel.read_text(), "do not touch")
            self.assertEqual(sorted(str(p) for p in decoy.rglob("*")), parent_files)
            report = json.loads(output.read_text())
            self.assertEqual(len(report["runs"]), 4)
            self.assertEqual(report["environment"]["qt_platform"], "offscreen")
            for run in report["runs"]:
                self.assertEqual(run["actual_viewport"], [1280, 800])
                self.assertEqual(len(run["samples"]["grab_ms"]), 20)
                self.assertEqual(len(run["samples"]["queued_callback_ms"]), 20)
                self.assertGreater(run["process_peak_rss_bytes"], 0)
                self.assertGreater(max(run["tracked_surface_bytes_per_frame"]), 0)
            rows = compare_reports(report, report)["comparisons"]
            self.assertTrue(all(row["p99_change_percent"] == 0 for row in rows))
            for key in ("environment", "workload", "schema_version"):
                changed = copy.deepcopy(report)
                changed[key] = "different"
                with self.assertRaisesRegex(ValueError, "Incompatible"):
                    compare_reports(report, changed)
            changed = copy.deepcopy(report)
            changed["runs"][0]["samples"]["grab_ms"].pop()
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                compare_reports(report, changed)
            changed = copy.deepcopy(report)
            changed["revision"]["harness_sha256"] = "changed"
            with self.assertRaisesRegex(ValueError, "measurement harness"):
                compare_reports(report, changed)
            changed = copy.deepcopy(report)
            changed["runs"].pop()
            with self.assertRaisesRegex(ValueError, "repetitions"):
                compare_reports(report, changed)


if __name__ == "__main__":
    unittest.main()
