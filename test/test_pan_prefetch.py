"""Prediction bounds and timing estimates, without a GUI or forecast files."""

import unittest

from gweatherrouting.rendering.panprefetch import (
    PanPreparationTime,
    pan_prefetch_offset,
)


class TestPanPreparationTime(unittest.TestCase):
    def test_unknown_workload_uses_fixed_one_second_fallback(self):
        timing = PanPreparationTime()
        self.assertEqual(timing.lookahead("source A"), 1)
        timing.observe("source A", 0.4)
        self.assertAlmostEqual(timing.lookahead("source A"), 0.6)
        self.assertEqual(timing.lookahead("source B"), 1)

    def test_smoothing_and_context_change(self):
        timing = PanPreparationTime()
        timing.observe("A", 0.4)
        timing.observe("A", 0.8)
        self.assertAlmostEqual(timing.seconds, 0.5)
        self.assertEqual(timing.samples, 2)
        timing.observe("B", 1.2)
        self.assertEqual(timing.seconds, 1.2)
        self.assertEqual(timing.samples, 1)

    def test_extremes_are_bounded(self):
        timing = PanPreparationTime()
        for duration, expected in ((0.001, 0.25), (100, 2)):
            timing.observe(str(duration), duration)
            self.assertEqual(timing.lookahead(str(duration)), expected)

    def test_invalid_measurements_do_not_train(self):
        timing = PanPreparationTime()
        for value in (0, -1, float("nan"), float("inf")):
            timing.observe("A", value)
        self.assertEqual(timing.samples, 0)
        self.assertIsNone(timing.seconds)


class TestPanPrefetchOffset(unittest.TestCase):
    def test_fixed_fallback_matches_existing_prediction(self):
        self.assertEqual(pan_prefetch_offset(300, -400, 1000, 800), (300, -400))
        self.assertIsNone(pan_prefetch_offset(100, 0, 1000, 800))

    def test_slow_jobs_predict_further_and_fast_jobs_do_not_overfetch(self):
        self.assertEqual(pan_prefetch_offset(200, 0, 1000, 800, 2), (400, 0))
        self.assertIsNone(pan_prefetch_offset(200, 0, 1000, 800, 0.5))
        # A slower pan can still travel beyond the reuse margin during a slow job.
        self.assertEqual(pan_prefetch_offset(100, 0, 1000, 800, 2), (200, 0))

    def test_diagonal_prediction_preserves_direction_and_cap(self):
        dx, dy = pan_prefetch_offset(-3000, 4000, 800, 1000, 2)
        self.assertAlmostEqual(dx, -450)
        self.assertAlmostEqual(dy, 600)

    def test_predictive_work_stays_inside_worker_margin_on_approach(self):
        self.assertEqual(
            pan_prefetch_offset(900, 0, 1000, 800, 2, max_pixels=256), (256, 0)
        )

    def test_bad_inputs_cannot_produce_requests(self):
        for args in (
            (0, 0, 800, 600, 1),
            (float("nan"), 0, 800, 600, 1),
            (300, 0, 0, 600, 1),
            (300, 0, 800, 600, -1),
            (300, 0, 800, 600, float("inf")),
        ):
            with self.subTest(args=args):
                self.assertIsNone(pan_prefetch_offset(*args))
