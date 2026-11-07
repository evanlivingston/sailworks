"""Benchmark protocol checks; performance thresholds are not portable CI tests."""

import importlib.util
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
