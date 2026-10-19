"""Differential weather-job reuse, budgets, fallback and resource ownership."""

from datetime import timedelta
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import patch

import numpy as np

from gweatherrouting.core.gribsampling import GridIndex, WeatherSampler
from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.rendering.overlayrenderer import RenderJob, RenderState
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windrenderer import WindOverlayRenderer
from test import test_grib_sampling as fixture


class TestWeatherJobResources(unittest.TestCase):
    setUp = fixture.TestGribSampling.setUp
    file = fixture.TestGribSampling.file
    field = fixture.TestGribSampling.field

    def test_resampled_vectors_with_no_complete_point_are_empty(self):
        values = np.array([[[1.0, np.nan], [np.nan, 2.0]]])
        self.assertEqual(
            WindOverlayRenderer._sampled_columns(
                values, np.array([0.0]), np.array([0.0, 1.0]), "wind"
            ),
            [],
        )

    def sources(self):
        split = [self.file([record]) for record in self.field(0) + self.field(3)]
        records = self.field(0, 4) + self.field(3, 4)
        for _, _, array in records:
            array[1, 1] = -9999
        fine = self.file(
            records,
            transform=(-0.25, 0.5, 0, -0.25, 0, 0.5),
            start=fixture.START + timedelta(minutes=10),
            process=84,
        )
        projected = self.file(
            self.field(0, 4) + self.field(3, 4),
            transform=(-25000, 50000, 0, -25000, 0, 50000),
            projection="EPSG:3857",
            process=125,
        )
        return split + [fine, projected]

    def job(self, sources):
        canvas = SimpleNamespace(
            _center_lat=0.5,
            _center_lon=0.5,
            _zoom=8,
            get_allocated_width=lambda: 320,
            get_allocated_height=lambda: 240,
            queue_draw=lambda: None,
        )
        return RenderJob(
            (),
            MapViewport(canvas),
            tuple((source,) for source in sources),
            1,
            (
                fixture.START + timedelta(minutes=30),
                0.8,
                True,
                True,
                True,
                1.0,
                0.85,
                True,
            ),
        )

    def renderer(self, reuse=True, source_limit=2):
        renderer = WindOverlayRenderer(WindPainter)
        renderer._source_retention_limit = (
            source_limit  # Isolate job-local reuse/eviction.
        )
        renderer._reuse_job_sources = reuse
        self.addCleanup(lambda: renderer.forget(object()))
        return renderer

    def test_pan_prediction_marker_does_not_change_rendered_fields_or_vectors(self):
        sources = self.sources()
        frames = []
        for predicted in (None, "pan_prediction", "zoom_prediction"):
            renderer = self.renderer()
            job = self.job(sources)
            if predicted is not None:
                setattr(job.viewport, predicted, True)
            image = renderer._render(job)
            frames.append((bytes(image.get_data()), job.barb_flat))
        for image, vectors in frames[1:]:
            self.assertEqual(frames[0][0], image)
            for before, after in zip(frames[0][1], vectors):
                np.testing.assert_array_equal(before, after)

    def test_terminal_close_releases_handles_and_field_cache(self):
        renderer = self.renderer()
        renderer._render(self.job(self.sources()))
        retained = list(renderer._gribs.values())
        self.assertTrue(retained)
        self.assertTrue(renderer._field_cache)
        renderer.close()
        renderer.close()
        self.assertTrue(all(provider.dataset is None for provider in retained))
        self.assertFalse(renderer._gribs)
        self.assertFalse(renderer._field_cache)
        self.assertEqual(renderer._field_cache_bytes, 0)

    def test_running_worker_releases_real_handles_without_ui_delivery(self):
        renderer = self.renderer()
        job = self.job(self.sources())
        entered, resume, released = Event(), Event(), Event()
        self.addCleanup(resume.set)
        providers, callbacks = [], []
        renderer.schedule = lambda *args: callbacks.append(args)
        release_resources = renderer._release_resources

        def release():
            release_resources()
            released.set()

        def render(_job):
            providers.append(renderer._worker_grib(job.charts[0][0]))
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("Test did not release worker")

        renderer._release_resources = release
        renderer._render_fields = render
        canvas = object()
        renderer._states[canvas] = RenderState(requested=job)
        renderer._pending[canvas] = job
        renderer._start_next()
        self.assertTrue(entered.wait(2))
        renderer.close()
        self.assertIsNotNone(providers[0].dataset)
        resume.set()
        self.assertTrue(released.wait(2))
        self.assertIsNone(providers[0].dataset)
        self.assertFalse(callbacks)
        self.assertFalse(renderer._gribs)

    def test_cached_and_uncached_mixed_fields_pixels_and_provenance_are_identical(self):
        sources = self.sources()
        results = []
        original_sample = WeatherSampler.sample
        original_index = GridIndex.__init__
        for reuse in (False, True):
            renderer = self.renderer(reuse)
            job = self.job(sources)
            samples, indices = [], []

            def sample(sampler, field, instant, lats, lons, cancel=lambda: False):
                result = original_sample(sampler, field, instant, lats, lons, cancel)
                samples.append(
                    (
                        field,
                        np.array(lats),
                        np.array(lons),
                        result.copy(),
                        sampler.point_sources.copy(),
                        set(sampler.last_sources),
                    )
                )
                return result

            def index(index, source):
                indices.append(source)
                original_index(index, source)

            with (
                patch.object(WeatherSampler, "sample", sample),
                patch.object(GridIndex, "__init__", index),
            ):
                image = renderer._render(job)
            self.assertIsNotNone(image)
            self.assertTrue(any(bytes(image.get_data())))
            self.assertIsNotNone(job.barb_flat)
            self.assertFalse(hasattr(job, "_weather_resources"))
            self.assertFalse(renderer._leased_providers)
            results.append(
                (
                    bytes(image.get_data()),
                    job.barb_flat,
                    samples,
                    job.weather_metrics,
                    len(indices),
                )
            )
        before, after = results
        self.assertEqual(before[0], after[0])
        for left, right in zip(before[1], after[1]):
            np.testing.assert_array_equal(left, right)
        self.assertEqual(len(before[2]), len(after[2]))
        self.assertGreaterEqual(len(after[2]), 4)  # Native fields AND mixed-grid fills.
        for left, right in zip(before[2], after[2]):
            self.assertEqual(left[0], right[0])
            for a, b in zip(left[1:5], right[1:5]):
                np.testing.assert_array_equal(a, b)
            self.assertEqual(left[5], right[5])
        self.assertEqual(after[3]["opens"], len(sources))
        self.assertGreater(before[3]["opens"], after[3]["opens"])
        self.assertEqual(after[4], len(sources))
        self.assertGreater(before[4], after[4])
        self.assertEqual(after[3]["samplers"], 1)
        self.assertEqual(before[3]["samplers"], 4)
        self.assertGreater(after[3]["provider_reuses"], before[3]["provider_reuses"])

    def test_zero_decode_retention_budget_does_not_change_output(self):
        sources = self.sources()
        full, bounded = self.renderer(), self.renderer()
        bounded._decoded_retention_limit = 0
        reference, candidate = self.job(sources), self.job(sources)
        expected = full._render(reference)
        actual = bounded._render(candidate)
        self.assertEqual(bytes(expected.get_data()), bytes(actual.get_data()))
        for left, right in zip(reference.barb_flat, candidate.barb_flat):
            np.testing.assert_array_equal(left, right)
        self.assertGreater(candidate.weather_metrics["decoded_evictions"], 0)
        self.assertGreater(candidate.weather_metrics["decoded_pass_peak_bytes"], 0)
        self.assertEqual(candidate.weather_metrics["decoded_retained_bytes"], 0)
        self.assertEqual(candidate.weather_metrics["opens"], len(sources))

    def test_cancelled_and_failed_jobs_release_all_nonretained_handles(self):
        sources = self.sources()
        for failure in (False, True):
            renderer = self.renderer()
            job = self.job(sources)
            opened = []
            original = renderer._worker_grib

            def worker(source):
                provider = original(source)
                opened.append(provider)
                return provider

            def interrupted(_job):
                if failure:
                    raise RuntimeError("interrupted weather job")
                job.cancel.set()
                return []

            with (
                patch.object(renderer, "_worker_grib", worker),
                patch.object(renderer, "_load_pressure", interrupted),
            ):
                if failure:
                    with self.assertRaisesRegex(RuntimeError, "interrupted"):
                        renderer._render(job)
                else:
                    self.assertIsNone(renderer._render(job))
            self.assertFalse(hasattr(job, "_weather_resources"))
            self.assertFalse(renderer._leased_providers)
            self.assertEqual(len(renderer._gribs), 2)
            self.assertTrue(all(provider.dataset is None for provider in opened[:-2]))
            renderer.forget(object())
            self.assertTrue(all(provider.dataset is None for provider in opened))

    def test_source_open_failure_is_retried_on_next_pass(self):
        sources = self.sources()
        renderer = self.renderer()
        job = self.job(sources)
        original = renderer._worker_grib
        attempts = {source: 0 for source in sources}

        def worker(source):
            attempts[source] += 1
            if source is sources[0] and attempts[source] == 1:
                raise ValueError("temporarily unavailable")
            return original(source)

        with patch.object(renderer, "_worker_grib", worker):
            self.assertIsNotNone(renderer._render(job))
        self.assertEqual(attempts[sources[0]], 2)
        self.assertTrue(all(attempts[source] == 1 for source in sources[1:]))
        self.assertFalse(renderer._leased_providers)

    def test_evicted_cross_job_index_cycles_are_closed_explicitly(self):
        sources = self.sources()
        renderer = self.renderer()
        previous = [renderer._worker_grib(source) for source in sources[:2]]
        for provider in previous:
            provider.sampling
        renderer._render(self.job(sources[2:]))
        self.assertTrue(all(provider.dataset is None for provider in previous))
        self.assertFalse(renderer._leased_providers)

    def test_failed_metadata_index_is_retried_without_reopening_the_source(self):
        sources = self.sources()
        renderer = self.renderer()
        job = self.job(sources)
        original = GridIndex.__init__
        attempts = []

        def index(index, source):
            attempts.append(source.path)
            if source.path == sources[0].path and attempts.count(source.path) == 1:
                raise ValueError("temporary metadata failure")
            original(index, source)

        with patch.object(GridIndex, "__init__", index):
            self.assertIsNotNone(renderer._render(job))
        self.assertEqual(attempts.count(sources[0].path), 2)
        self.assertEqual(job.weather_metrics["samplers"], 2)
        self.assertEqual(job.weather_metrics["opens"], len(sources))
        self.assertFalse(renderer._leased_providers)

    def test_second_view_reuses_retained_handles_and_decoded_bands(self):
        sources = self.sources()
        renderer = self.renderer(source_limit=64)
        first = self.job(sources)
        self.assertIsNotNone(renderer._render(first))
        self.assertEqual(first.weather_metrics["opens"], len(sources))
        self.assertGreater(renderer.cache_state["retained_sources"], 2)
        second = self.job(sources)
        # Shift the viewport so the second pass needs the same sources again.
        second.viewport._center_lat += 0.5
        second.viewport._center_lon += 0.5
        self.assertIsNotNone(renderer._render(second))
        self.assertEqual(second.weather_metrics["opens"], 0)
        self.assertGreater(second.weather_metrics["provider_reuses"], 0)
        self.assertFalse(renderer._leased_providers)
        # Deselection closes handles instead of leaking them in the larger cache.
        removed = self.job(sources[1:])
        self.assertIsNotNone(renderer._render(removed))
        self.assertNotIn(sources[0], renderer._gribs)
        self.assertFalse(renderer._leased_providers)

    def test_contained_field_cache_reuses_return_views_without_overfetch(self):
        from gweatherrouting.core.gribsampling import WeatherSampler

        sources = self.sources()
        renderer = self.renderer(source_limit=64)
        first = self.job(sources)
        reference = renderer._render(first)
        self.assertIsNotNone(reference)
        calls = []
        original = WeatherSampler.grid

        def counting(sampler, field, instant, bounds, cancel=lambda: False):
            calls.append((field, tuple(bounds[0]), tuple(bounds[1])))
            return original(sampler, field, instant, bounds, cancel)

        with patch.object(WeatherSampler, "grid", counting):
            # Identical viewport reuses cached native columns exactly.
            replay = self.job(sources)
            replay.viewport = first.viewport
            replay.palette = first.palette
            replay.charts = first.charts
            candidate = renderer._render(replay)
            self.assertEqual(bytes(candidate.get_data()), bytes(reference.get_data()))
            for left, right in zip(first.barb_flat, replay.barb_flat):
                np.testing.assert_array_equal(left, right)
            self.assertGreater(replay.weather_metrics["field_cache"]["hits"], 0)
            self.assertEqual(len(calls), 0)
            # A shifted viewport samples only newly exposed strips; the
            # merged columns must equal a fresh full-bounds sample.
            panned = self.job(sources)
            panned.viewport._center_lat = first.viewport._center_lat + 0.05
            panned.viewport._center_lon = first.viewport._center_lon + 0.05
            image = renderer._render(panned)
            self.assertIsNotNone(image)
            exact = renderer._viewport_bounds(panned)
            (south, west), (north, east) = exact
            for _field, corner, opposite in calls:
                self.assertGreaterEqual(corner[0], south - 1e-9)
                self.assertGreaterEqual(corner[1], west - 1e-9)
                self.assertLessEqual(opposite[0], north + 1e-9)
                self.assertLessEqual(opposite[1], east + 1e-9)
            self.assertGreater(panned.weather_metrics["field_strips_sampled"], 0)
            fresh = self.renderer(source_limit=64)
            fresh._field_cache_enabled = False
            reference_job = self.job(sources)
            reference_job.viewport._center_lat = panned.viewport._center_lat
            reference_job.viewport._center_lon = panned.viewport._center_lon
            expected = fresh._render(reference_job)
            self.assertEqual(bytes(image.get_data()), bytes(expected.get_data()))
            for left, right in zip(panned.barb_flat, reference_job.barb_flat):
                np.testing.assert_array_equal(left, right)
            # Returning to the original viewport reuses the first entry.
            home = self.job(sources)
            home.viewport = first.viewport
            home.palette = first.palette
            home.charts = first.charts
            settled = len(calls)
            returned = renderer._render(home)
            self.assertEqual(bytes(returned.get_data()), bytes(reference.get_data()))
            self.assertEqual(len(calls), settled)
            self.assertGreater(home.weather_metrics["field_cache"]["hits"], 0)

    def test_rect_subtraction_partitions_without_gaps_or_overlap(self):
        renderer = self.renderer()
        outer = ((0.0, 0.0), (4.0, 6.0))
        self.assertEqual(renderer._uncovered_rects(outer, []), [outer])
        self.assertEqual(renderer._uncovered_rects(outer, [outer]), [])
        # Central cover splits the remainder into four exact strips.
        remaining = renderer._uncovered_rects(outer, [((1.0, 1.0), (3.0, 5.0))])
        area = sum((n - s) * (e - w) for (s, w), (n, e) in remaining)
        self.assertAlmostEqual(area, 4.0 * 6.0 - 2.0 * 4.0)
        for (south, west), (north, east) in remaining:
            self.assertGreater(north, south)
            self.assertGreater(east, west)
        # Union of two overlapping covers leaves an L-shaped remainder.
        remaining = renderer._uncovered_rects(
            outer, [((0.0, 0.0), (4.0, 3.0)), ((0.0, 3.0), (2.0, 6.0))]
        )
        area = sum((n - s) * (e - w) for (s, w), (n, e) in remaining)
        self.assertAlmostEqual(area, 2.0 * 3.0)
        self.assertFalse(renderer._rect_overlap(outer, ((5.0, 5.0), (6.0, 6.0))))
        self.assertTrue(renderer._rect_overlap(outer, ((3.9, 5.9), (5.0, 7.0))))

    def test_disabled_field_cache_forces_full_resampling(self):
        sources = self.sources()
        renderer = self.renderer(source_limit=64)
        renderer._field_cache_enabled = False
        job = self.job(sources)
        self.assertIsNotNone(renderer._render(job))
        self.assertEqual(
            job.weather_metrics["field_cache"], dict(hits=0, misses=0, evictions=0)
        )

    def test_empty_style_and_precancelled_job_do_not_open_sources(self):
        sources = self.sources()
        renderer = self.renderer()
        job = self.job(sources)
        job.palette = (job.palette[0], 0.8, True, False, False)
        self.assertIsNotNone(renderer._render(job))
        self.assertEqual(job.weather_metrics["opens"], 0)
        cancelled = self.job(sources)
        cancelled.cancel.set()
        self.assertIsNone(renderer._render(cancelled))
        self.assertEqual(cancelled.weather_metrics["opens"], 0)
        self.assertFalse(renderer._leased_providers)
