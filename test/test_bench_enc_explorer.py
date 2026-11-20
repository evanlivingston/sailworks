"""Unit tests for the pure geometry and metric code in bench_enc_explorer."""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bench_enc_explorer as bench


def frame(lat, lon, zoom, width=1000.0, height=800.0):
    return {"lat": lat, "lon": lon, "zoom": zoom, "width": width, "height": height}


class TestCoverageGeometry(unittest.TestCase):
    def test_nothing_presented_uncovers_the_whole_viewport(self):
        self.assertEqual(
            bench.uncovered_fraction(None, frame(37.8, -122.4, 10.0), 256.0), 1.0
        )

    def test_same_camera_is_fully_covered(self):
        anchor = frame(37.81, -122.42, 10.0)
        self.assertEqual(bench.uncovered_fraction(anchor, anchor, 256.0), 0.0)

    def test_pan_inside_the_margin_is_still_covered(self):
        anchor = frame(37.81, -122.42, 10.0)
        panned = bench.pan_camera(anchor, 200.0, 0.0)
        self.assertEqual(bench.uncovered_fraction(anchor, panned, 256.0), 0.0)

    def test_pan_past_the_margin_exposes_stripes(self):
        anchor = frame(37.81, -122.42, 10.0)
        # The composed frame spans [-margin, width + margin]; a pan of
        # width/2 + margin leaves exactly half the viewport uncovered.
        panned = bench.pan_camera(anchor, 500.0 + 256.0, 0.0)
        self.assertEqual(bench.uncovered_fraction(anchor, panned, 256.0), 0.5)

    def test_vertical_and_horizontal_pans_combine(self):
        anchor = frame(37.81, -122.42, 10.0)
        panned = bench.pan_camera(anchor, 500.0 + 256.0, 400.0 + 256.0)
        # Half the width and half the height remain: one quarter covered.
        self.assertAlmostEqual(
            bench.uncovered_fraction(anchor, panned, 256.0), 0.75, places=9
        )

    def test_zoom_in_covers_and_zoom_out_exposes(self):
        anchor = frame(37.81, -122.42, 10.0)
        zoomed_in = frame(37.81, -122.42, 11.0)
        self.assertEqual(bench.uncovered_fraction(anchor, zoomed_in, 256.0), 0.0)
        zoomed_out = frame(37.81, -122.42, 9.0)
        # At half scale the composition spans 756 px across and 656 px down
        # (the margin is added before scaling).
        self.assertAlmostEqual(
            bench.uncovered_fraction(anchor, zoomed_out, 256.0),
            1.0 - (756.0 / 1000.0) * (656.0 / 800.0),
        )

    def test_size_change_uncovers_the_new_area(self):
        anchor = frame(37.81, -122.42, 10.0)
        resized = frame(37.81, -122.42, 10.0, width=1200.0)
        self.assertAlmostEqual(
            bench.uncovered_fraction(anchor, resized, 0.0),
            1.0 - 1000.0 / 1200.0,
            places=9,
        )

    def test_margin_is_never_reported_as_negative_coverage(self):
        anchor = frame(37.81, -122.42, 10.0)
        panned = bench.pan_camera(anchor, 100000.0, 0.0)
        self.assertEqual(bench.uncovered_fraction(anchor, panned, 256.0), 1.0)


class TestWorldMath(unittest.TestCase):
    def test_world_centre_round_trip(self):
        for lat, lon, zoom in ((37.81, -122.42, 10.0), (-33.9, 151.2, 3.5)):
            x = bench.world_x(lon, zoom)
            y = bench.world_y(lat, zoom)
            back_lat, back_lon = bench.centre_at_world(x, y, zoom)
            self.assertAlmostEqual(back_lat, lat, places=9)
            self.assertAlmostEqual(back_lon, lon, places=9)

    def test_positive_pan_moves_the_camera_west(self):
        anchor = frame(37.81, -122.42, 10.0)
        moved = bench.pan_camera(anchor, 15.0, 0.0)
        self.assertLess(moved["lon"], anchor["lon"])
        self.assertAlmostEqual(
            bench.world_x(anchor["lon"], anchor["zoom"])
            - bench.world_x(moved["lon"], moved["zoom"]),
            15.0,
            places=6,
        )

    def test_latitude_is_clamped_for_panning(self):
        self.assertTrue(math.isfinite(bench.world_y(90.0, 5.0)))
        self.assertTrue(math.isfinite(bench.world_y(-90.0, 5.0)))


class TestSummaries(unittest.TestCase):
    def test_nearest_rank_percentile(self):
        values = [4.0, 1.0, 3.0, 2.0]
        self.assertEqual(bench.percentile(values, 0.0), 1.0)
        self.assertEqual(bench.percentile(values, 0.5), 3.0)
        self.assertEqual(bench.percentile(values, 1.0), 4.0)
        self.assertIsNone(bench.percentile([], 0.5))

    def test_summarise_ms_is_milliseconds(self):
        summary = bench.summarise_ms([0.010, 0.020, 0.030])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["p50"], 20.0)
        self.assertEqual(summary["max"], 30.0)
        self.assertEqual(summary["mean"], 20.0)
        self.assertIsNone(bench.summarise_ms([]))


class TestScriptedScenarios(unittest.TestCase):
    def test_pan_slow_is_900_pixels_over_60_frames(self):
        steps = bench.build_steps("pan_slow")
        self.assertEqual(len(steps), 60)
        anchor = {"lat": 37.81, "lon": -122.42, "zoom": 10.0}
        state = anchor
        for step in steps:
            state = bench._advance(state, step)
        moved = bench.world_x(anchor["lon"], anchor["zoom"]) - bench.world_x(
            state["lon"], state["zoom"]
        )
        self.assertAlmostEqual(moved, 900.0, places=5)

    def test_pan_fast_is_900_pixels_over_20_frames(self):
        steps = bench.build_steps("pan_fast")
        self.assertEqual(len(steps), 20)
        self.assertTrue(all(step["pan"] == (45.0, 0.0) for step in steps))

    def test_zoom_out_has_three_steps_each_settling(self):
        steps = bench.build_steps("zoom_out")
        zooms = [step for step in steps if step["kind"] == "zoom"]
        self.assertEqual(len(zooms), 3)
        self.assertEqual(len(steps), 3 * (1 + bench.ZOOM_SETTLE_FRAMES))
        anchor = {"lat": 37.81, "lon": -122.42, "zoom": 10.0}
        state = anchor
        for step in steps:
            state = bench._advance(state, step)
        self.assertAlmostEqual(state["zoom"], 8.5)

    def test_pan_zoom_settle_pans_zooms_and_pans_back(self):
        steps = bench.build_steps("pan_zoom_settle")
        kinds = [step["kind"] for step in steps]
        self.assertEqual(kinds.count("zoom"), 1)
        self.assertEqual(kinds.count("settle"), bench.PAN_ZOOM_SETTLE_FRAMES)
        self.assertEqual(steps[0]["pan"], (20.0, 0.0))
        self.assertEqual(steps[-1]["pan"], (-20.0, 0.0))
        self.assertEqual(len(steps), 20 + 1 + bench.PAN_ZOOM_SETTLE_FRAMES + 20)

    def test_scenarios_are_repeatable(self):
        for name in bench.SCENARIOS:
            self.assertEqual(bench.build_steps(name), bench.build_steps(name))
        with self.assertRaises(ValueError):
            bench.build_steps("does-not-exist")


class TestRingOverride(unittest.TestCase):
    """The ring override is inert by default and equivalent to the env var."""

    def setUp(self):
        import os

        from sailworks.qt.viewporttransform import ENC_COMPOSE_MARGIN_ENV

        self.env = ENC_COMPOSE_MARGIN_ENV
        self._os = os
        self._saved = os.environ.pop(self.env, None)

    def tearDown(self):
        self._os.environ.pop(self.env, None)
        if self._saved is not None:
            self._os.environ[self.env] = self._saved

    def test_no_option_applies_no_override(self):
        margin = bench.parse_margin_list(None, None)
        self.assertEqual(margin, [None])
        bench.apply_margin_override(margin[0])
        self.assertNotIn(self.env, self._os.environ)

    def test_margin_option_sets_the_environment_variable(self):
        margins = bench.parse_margin_list(None, 900.0)
        self.assertEqual(margins, [900.0])
        bench.apply_margin_override(margins[0])
        self.assertEqual(self._os.environ[self.env], "900")

    def test_matrix_option_parses_a_list(self):
        self.assertEqual(
            bench.parse_margin_list("256, 400 ,600", None),
            [256.0, 400.0, 600.0],
        )

    def test_empty_matrix_is_rejected(self):
        with self.assertRaises(ValueError):
            bench.parse_margin_list("", None)

    def test_bad_rings_are_rejected(self):
        for matrix, margin in (("256,wide", None), ("0", None), ("-1", None)):
            with self.assertRaises(ValueError):
                bench.parse_margin_list(matrix, margin)
        with self.assertRaises(ValueError):
            bench.parse_margin_list(None, 0.0)

    def test_geometry_estimate_is_labelled_and_stride_based(self):
        estimate = bench._geometry_estimate(
            {"cells": 12, "vertices": 1000, "triangles": 500}
        )
        self.assertTrue(estimate["estimate"])
        self.assertEqual(estimate["vertices"], 1000)
        self.assertEqual(estimate["triangles"], 500)
        self.assertEqual(
            estimate["estimated_bytes"],
            1000 * bench.VERTEX_STRIDE_BYTES * bench.MEMORY_COPY_MULTIPLIER,
        )

    def test_prefetch_budget_shrinks_the_ring_by_the_trigger(self):
        # 45 px/frame at 60 Hz is 2.7 px/ms; half of a 400 px ring is 74.1 ms.
        self.assertAlmostEqual(bench.prefetch_budget_ms(400.0), 200.0 / 2.7, places=6)
        self.assertLess(
            bench.prefetch_budget_ms(256.0), bench.prefetch_budget_ms(900.0)
        )


class TestTargetsAndSchema(unittest.TestCase):
    def test_current_target_is_supported(self):
        self.assertEqual(bench.resolve_target("current"), "current")

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ValueError):
            bench.resolve_target("resident-geometry")

    def test_schema_version_is_documented(self):
        self.assertEqual(bench.SCHEMA_VERSION, 1)
        self.assertIn("current", bench.TARGETS)
        self.assertEqual(len(bench.SCENARIOS), 4)


if __name__ == "__main__":
    unittest.main()
