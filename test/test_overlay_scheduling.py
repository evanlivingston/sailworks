"""Deterministic weather scheduling/coverage tests without a GUI event loop."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import cairo

from gweatherrouting.core.mapviewport import MercatorProjection, MapViewport
from gweatherrouting.rendering.overlayrenderer import MARGIN, covers
from gweatherrouting.rendering.windrenderer import WindOverlayRenderer


class Canvas(MercatorProjection):
    _center_lat = 0
    _center_lon = 0
    _zoom = 8
    _zoom_animating = False
    width, height, factor = 320, 240, 1

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height

    def get_scale_factor(self):
        return self.factor

    def queue_draw(self):
        pass

    def pan(self, pixels):
        self._center_lon = pixels * 360 / (256 * 2**self._zoom)


class Renderer(WindOverlayRenderer):
    """Run the production queue/publication policy on a manually driven worker."""

    def __init__(self, legacy=False, preserve=False):
        super().__init__(None, lambda *args: None)
        self.started = []
        self.events = []
        self._coalesce_active = not legacy
        self._preserve_covered_jobs = preserve and not legacy

    def _schedule_event(self, event, job):
        self.events.append((event, job.request_id, time.monotonic()))

    def _start_next(self):
        # Manual worker: visible first, speculation only while globally idle.
        if self._running:
            return
        if self._pending:
            canvas, job = self._pending.popitem(last=False)
            self._running = True
            self._active_canvas, self._active_job = canvas, job
        elif self._speculative:
            canvas, job = next(iter(self._speculative.items()))
            del self._speculative[canvas]
            self._speculative_active = (canvas, job)
            self._running = True
        else:
            return
        job.started_at = time.monotonic()
        self.started.append(job)
        self._schedule_event("start", job)

    def finish(self):
        if self._active_job is not None:
            canvas, job = self._active_canvas, self._active_job
        else:
            canvas, job = self._speculative_active
        job.finished_at = time.monotonic()
        self._schedule_event("finish", job)
        image = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            job.viewport.width * job.factor,
            job.viewport.height * job.factor,
        )
        self._completed(canvas, job, image, None)
        return job


class TestOverlayScheduling(unittest.TestCase):
    def setUp(self):
        self.canvas = Canvas()
        self.renderer = Renderer()
        self.sources = (("forecast",),)
        self.style = (datetime(2026, 1, 1), 0.8, True, True, True)
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 240)
        self.cr = cairo.Context(self.surface)

    def draw(self, renderer=None, canvas=None):
        (renderer or self.renderer).draw(
            canvas or self.canvas, self.cr, self.sources, self.style, live_barbs=False
        )

    def prefetch_view(self, latitude, longitude, renderer=None, canvas=None):
        renderer = renderer or self.renderer
        canvas = canvas or self.canvas
        viewport = MapViewport(canvas, MARGIN)
        viewport._center_lat, viewport._center_lon = latitude, longitude
        state = renderer._states.get(canvas)
        key = state.key if state else (self.style, 1, tuple(c[0] for c in self.sources))
        return renderer.prefetch(canvas, key, viewport, self.sources, 1, self.style)

    def prefetch_zoom(self, zoom, longitude=0):
        viewport = MapViewport(self.canvas, MARGIN)
        viewport._zoom = zoom
        viewport._center_lon = longitude
        viewport.zoom_prediction = True
        key = self.renderer._states[self.canvas].key
        return self.renderer.prefetch(
            self.canvas, key, viewport, self.sources, 1, self.style
        )

    def test_zoom_speculation_replaces_changed_target_and_anchor(self):
        self.draw()
        self.renderer.finish()
        target = self.canvas._zoom + 1
        self.assertTrue(self.prefetch_zoom(target))
        job = self.renderer._speculative_active[1]
        requests = self.renderer.prefetch_metrics["requests"]
        self.assertTrue(self.prefetch_zoom(target))
        self.assertEqual(self.renderer.prefetch_metrics["requests"], requests)
        self.assertFalse(job.cancel.is_set())
        self.assertTrue(self.prefetch_zoom(target + 1))
        self.assertTrue(job.cancel.is_set())
        replacement = self.renderer._speculative[self.canvas]
        self.renderer.finish()
        self.assertIs(self.renderer._speculative_active[1], replacement)
        self.assertTrue(self.prefetch_zoom(target + 1, longitude=1))
        self.assertTrue(replacement.cancel.is_set())

    def test_zoom_prediction_adopts_at_target_not_intermediate_scale(self):
        self.draw()
        self.renderer.finish()
        target = self.canvas._zoom + 1
        self.prefetch_zoom(target)
        job = self.renderer._speculative_active[1]
        self.canvas._zoom_animating = True
        self.canvas._zoom += 0.5
        self.draw()
        self.assertIs(self.renderer._speculative_active[1], job)
        self.assertFalse(job.cancel.is_set())
        self.canvas._zoom = target
        self.canvas._zoom_animating = False
        self.draw()
        self.assertIs(self.renderer._active_job, job)
        self.assertFalse(self.renderer._pending)
        self.renderer.finish()
        self.assertIs(self.renderer._states[self.canvas].frame[0], job)

    def test_zoom_return_to_prepared_camera_does_not_render_duplicate(self):
        self.draw()
        self.renderer.finish()
        self.assertFalse(self.prefetch_zoom(self.canvas._zoom))
        self.assertFalse(self.renderer._speculative)
        self.assertIsNone(self.renderer._speculative_active)

    def test_pan_lookahead_learns_execution_not_queue_or_publication_delay(self):
        self.draw()
        job = self.renderer._active_job
        job.created_at, job.started_at, job.finished_at = 1, 10, 10.4
        self.renderer._completed(self.canvas, job, self.surface, None)
        self.assertAlmostEqual(self.renderer.pan_lookahead(self.canvas), 0.6)
        self.assertEqual(self.renderer._pan_preparation.samples, 1)
        self.canvas._zoom += 1
        self.assertEqual(self.renderer.pan_lookahead(self.canvas), 1)

    def test_predicted_pan_work_is_adopted_while_it_encloses_current_view(self):
        self.canvas.is_moving = lambda: True
        self.draw()
        self.renderer.finish()
        longitude = 256 * 360 / (256 * 2**self.canvas._zoom)
        self.assertTrue(self.prefetch_view(0, longitude))
        job = self.renderer._speculative_active[1]
        job.viewport.pan_prediction = True
        self.canvas.pan(32)  # 224px from prediction: enclosed, not inside fits().
        self.draw()
        self.assertIs(self.renderer._active_job, job)
        self.assertFalse(job.cancel.is_set())
        self.assertFalse(self.renderer._pending)
        self.canvas.pan(448)
        self.draw()
        self.assertFalse(job.cancel.is_set())
        self.renderer.finish()
        self.assertIs(self.renderer._states[self.canvas].frame[0], job)
        self.draw()
        self.assertFalse(self.renderer._pending)
        self.assertIsNone(self.renderer._active_job)
        self.canvas.pan(513)  # Actual pixel enclosure, not an unbounded survival.
        self.draw()
        self.assertIsNotNone(self.renderer._active_job)

    def test_predicted_work_uses_normal_refresh_threshold_after_pan_stops(self):
        self.canvas.is_moving = lambda: True
        self.draw()
        self.renderer.finish()
        job = self.renderer._states[self.canvas].frame[0]
        job.viewport.pan_prediction = True
        self.canvas.pan(192)
        self.draw()
        self.assertIsNone(self.renderer._active_job)
        self.canvas.is_moving = lambda: False
        self.draw()
        self.assertIsNotNone(self.renderer._active_job)

    def test_predicted_pan_never_adopts_other_content_or_zoom(self):
        for change in ("style", "source", "dpr", "zoom", "reversal"):
            with self.subTest(change=change):
                self.setUp()
                self.canvas.is_moving = lambda: True
                self.draw()
                self.renderer.finish()
                longitude = 256 * 360 / (256 * 2**self.canvas._zoom)
                self.prefetch_view(0, longitude)
                job = self.renderer._speculative_active[1]
                job.viewport.pan_prediction = True
                if change == "style":
                    self.style = (*self.style[:1], 0.4, *self.style[2:])
                elif change == "source":
                    self.sources = (("other forecast",),)
                elif change == "dpr":
                    self.canvas.factor = 2
                elif change == "zoom":
                    self.canvas._zoom += 1
                else:
                    self.canvas.pan(-160)
                self.draw()
                self.assertTrue(job.cancel.is_set())
                self.assertEqual(self.renderer.prefetch_metrics["adopts"], 0)

    def test_cancelled_failed_and_empty_jobs_do_not_train_pan_prediction(self):
        for result in ("cancelled", "failed", "empty"):
            with self.subTest(result=result):
                self.setUp()
                self.draw()
                job = self.renderer._active_job
                job.started_at, job.finished_at = 10, 20
                if result == "cancelled":
                    job.cancel.set()
                self.renderer._completed(
                    self.canvas,
                    job,
                    None if result == "empty" else self.surface,
                    "failed" if result == "failed" else None,
                )
                self.assertEqual(self.renderer._pan_preparation.samples, 0)
                self.assertEqual(self.renderer.pan_lookahead(self.canvas), 1)

    def test_forgetting_last_view_drops_timing_context(self):
        self.draw()
        job = self.renderer._active_job
        job.started_at, job.finished_at = 10, 10.4
        self.renderer._completed(self.canvas, job, self.surface, None)
        self.assertEqual(self.renderer._pan_preparation.samples, 1)
        self.renderer.forget(self.canvas)
        self.assertEqual(self.renderer._pan_preparation.samples, 0)
        self.assertIsNone(self.renderer._pan_preparation.key)

    def test_covers_uses_actual_extent_and_requires_native_zoom(self):
        view = MapViewport(self.canvas, 256)
        for offset, expected in (
            (0, True),
            (128, True),
            (160, True),
            (256, True),
            (256.5, False),
        ):
            self.canvas.pan(offset)
            self.assertEqual(covers(view, self.canvas), expected, offset)
        self.canvas.pan(0)
        self.canvas.width += 100
        self.assertTrue(covers(view, self.canvas))
        self.canvas.width = 900
        self.assertFalse(covers(view, self.canvas))
        self.canvas.width = 320
        self.canvas._zoom += 0.1
        self.assertFalse(covers(view, self.canvas))

    def test_useful_active_job_survives_refresh_threshold_with_bounded_pending(self):
        self.renderer = Renderer(preserve=True)  # Rejected trial, never default.
        self.draw()
        running = self.renderer._active_job
        for pixels in (160, 200, 224, 256):
            self.canvas.pan(pixels)
            self.draw()
            self.assertFalse(running.cancel.is_set())
            self.assertLessEqual(len(self.renderer._pending), 1)
        self.renderer.finish()
        self.assertIs(self.renderer._states[self.canvas].frame[0], running)
        self.assertTrue(self.renderer.prepared_view_status(self.canvas)["current"])

    def test_detached_active_job_is_cancelled_when_view_or_content_is_obsolete(self):
        for change in ("far pan", "style", "source", "dpr"):
            with self.subTest(change=change):
                self.setUp()
                self.renderer = Renderer(preserve=True)
                self.draw()
                running = self.renderer._active_job
                self.canvas.pan(160)
                self.draw()
                self.assertIsNot(self.renderer._states[self.canvas].requested, running)
                if change == "far pan":
                    self.canvas.pan(600)
                elif change == "style":
                    self.style = (self.style[0], 0.3, True, True, True)
                elif change == "source":
                    self.sources = (("different forecast",),)
                else:
                    self.canvas.factor = 2
                self.draw()
                self.assertTrue(running.cancel.is_set())
                self.assertIsNotNone(running.cancelled_at)
                self.renderer.finish()
                self.assertIsNone(self.renderer._states[self.canvas].frame)

    def test_reversal_reattaches_active_and_discards_duplicate_queued_work(self):
        self.draw()
        running = self.renderer._active_job
        original = self.style
        self.style = (self.style[0] + timedelta(hours=1), *self.style[1:])
        self.draw()
        queued = self.renderer._pending[self.canvas]
        self.style = original
        self.draw()
        self.assertIs(self.renderer._states[self.canvas].requested, running)
        self.assertFalse(self.renderer._pending)
        self.assertTrue(queued.cancel.is_set())
        self.assertEqual(queued.cancel_reason, "covered by active request")
        self.renderer.finish()
        self.assertFalse(self.renderer._running)
        self.assertEqual(len(self.renderer.started), 1)

    def test_latest_forecast_remains_queued_while_old_time_can_publish_preview(self):
        self.draw()
        running = self.renderer._active_job
        self.style = (self.style[0] + timedelta(hours=1), *self.style[1:])
        self.draw()
        latest = self.renderer._pending[self.canvas]
        self.draw()
        self.assertIs(self.renderer._pending[self.canvas], latest)
        self.assertFalse(running.cancel.is_set())
        self.renderer.finish()
        self.assertEqual(
            self.renderer.prepared_view_status(self.canvas),
            dict(preview=True, current=False),
        )
        self.assertIs(self.renderer._active_job, latest)
        self.renderer.finish()
        self.assertTrue(self.renderer.prepared_view_status(self.canvas)["current"])

    def test_forget_cancels_both_active_and_queued_without_late_publication(self):
        self.draw()
        running = self.renderer._active_job
        self.canvas.pan(160)
        self.draw()
        queued = self.renderer._pending[self.canvas]
        self.renderer.forget(self.canvas)
        self.assertTrue(running.cancel.is_set())
        self.assertTrue(queued.cancel.is_set())
        self.renderer.finish()
        self.assertNotIn(self.canvas, self.renderer._states)
        self.assertFalse(self.renderer._pending)

    def test_cancelled_matching_request_does_not_suppress_a_replacement(self):
        self.draw()
        old = self.renderer._active_job
        self.renderer._cancel(old, "test cancellation")
        self.draw()
        self.assertIsNot(self.renderer._states[self.canvas].requested, old)
        self.assertEqual(len(self.renderer._pending), 1)

    def test_another_maps_visible_work_is_not_removed_by_coalescing(self):
        other = Canvas()
        self.draw()
        self.draw(canvas=other)
        original = self.style
        self.style = (self.style[0] + timedelta(hours=1), *self.style[1:])
        self.draw()
        self.style = original
        self.draw()
        self.assertEqual(list(self.renderer._pending), [other])
        self.renderer.finish()
        self.assertIs(self.renderer._active_canvas, other)

    def test_prefetch_promotes_on_arrival_without_visible_work(self):
        self.draw()
        self.renderer.finish()
        self.assertTrue(self.prefetch_view(0, 1.0))
        self.assertEqual(len(self.renderer.started), 2)
        self.renderer.finish()
        state = self.renderer._states[self.canvas]
        self.assertEqual(len(state.prefetched), 1)
        predicted = state.prefetched[0][0]
        # Arrival at the predicted view promotes with no new worker job.
        self.canvas.pan(1.0 * 256 * 2**self.canvas._zoom / 360)
        self.draw()
        self.assertEqual(len(self.renderer.started), 2)
        self.assertIs(self.renderer._states[self.canvas].frame[0], predicted)
        self.assertEqual(self.renderer.prefetch_metrics["hits"], 1)

    def test_visible_demand_cancels_stale_speculation_without_starvation(self):
        self.draw()
        running = self.renderer._active_job
        self.assertTrue(self.prefetch_view(0, 5.0))
        # Queued speculation waits while visible work runs.
        self.assertIs(self.renderer._active_job, running)
        self.assertFalse(running.speculative)
        self.assertEqual(list(self.renderer._speculative), [self.canvas])
        self.renderer.finish()
        # Visible published; speculation starts only now that work is idle.
        spec = self.renderer._speculative_active[1]
        self.assertTrue(spec.speculative)
        # A content change kills running speculation immediately.
        self.style = (self.style[0] + timedelta(hours=1), *self.style[1:])
        self.draw()
        self.assertTrue(spec.cancel.is_set())
        self.renderer.finish()
        self.renderer.finish()
        self.assertTrue(self.renderer.prepared_view_status(self.canvas)["current"])

    def test_new_visible_work_preempts_drifted_speculation(self):
        self.draw()
        self.renderer.finish()
        self.assertTrue(self.prefetch_view(0, 5.0))
        spec = self.renderer._speculative_active[1]
        # Drifted same-content view with a stale frame: visible work wins.
        self.canvas.pan(300)
        self.draw()
        self.assertTrue(spec.cancel.is_set())
        self.assertEqual(spec.cancel_reason, "visible demand")
        self.renderer.finish()
        self.renderer.finish()
        self.assertTrue(self.renderer.prepared_view_status(self.canvas)["current"])

    def test_prefetch_store_is_bounded_and_forget_drops_it(self):
        self.draw()
        self.renderer.finish()
        self.renderer._prefetch_store_limit = 1
        self.assertTrue(self.prefetch_view(0, 1.0))
        self.renderer.finish()
        self.assertTrue(self.prefetch_view(0, 2.0))
        self.renderer.finish()
        state = self.renderer._states[self.canvas]
        self.assertLessEqual(len(state.prefetched), 1)
        self.assertGreaterEqual(self.renderer.prefetch_metrics["evictions"], 1)
        self.renderer.forget(self.canvas)
        self.assertNotIn(self.canvas, self.renderer._states)
        self.assertFalse(self.renderer._speculative)
        self.assertIsNone(self.renderer._speculative_active)

    def test_opt_in_trial_controlled_reversal_halves_first_prepared_view_latency(
        self,
    ):
        outcomes = []
        for legacy in (True, False):
            clock = SimpleNamespace(now=0.0)
            renderer, canvas = Renderer(legacy, preserve=True), Canvas()
            with patch(
                "gweatherrouting.rendering.overlayrenderer.time.monotonic",
                side_effect=lambda: clock.now,
            ):
                self.draw(renderer, canvas)
                clock.now = 0.02
                canvas.pan(160)
                self.draw(renderer, canvas)
                clock.now = 0.03
                canvas.pan(0)
                self.draw(renderer, canvas)
                # Simulate a 100ms uninterruptible decode/portrayal stage.
                clock.now = 0.1
                first = renderer.finish()
                if not renderer.prepared_view_status(canvas)["current"]:
                    clock.now = 0.2
                    renderer.finish()
                outcomes.append(
                    (clock.now, len(renderer.started), first.cancel.is_set())
                )
        self.assertEqual(outcomes, [(0.2, 2, True), (0.1, 1, False)])

    def test_default_coalescing_eliminates_duplicate_return_time_job(self):
        outcomes = []
        for legacy in (True, False):
            self.setUp()
            renderer = Renderer(legacy)
            original = self.style
            self.draw(renderer)
            self.style = (self.style[0] + timedelta(hours=1), *self.style[1:])
            self.draw(renderer)
            self.style = original
            self.draw(renderer)
            renderer.finish()
            self.assertTrue(renderer.prepared_view_status(self.canvas)["current"])
            outcomes.append((len(renderer.started), renderer._running))
        self.assertEqual(outcomes, [(2, True), (1, False)])

    def test_source_change_during_zoom_discards_incompatible_queued_work(self):
        self.draw()
        running = self.renderer._active_job
        self.style = (self.style[0] + timedelta(hours=1), *self.style[1:])
        self.draw()
        queued = self.renderer._pending[self.canvas]
        self.canvas._zoom_animating = True
        self.sources = (("replacement source",),)
        self.draw()
        self.assertTrue(running.cancel.is_set())
        self.assertTrue(queued.cancel.is_set())
        self.assertEqual(queued.cancel_reason, "obsolete queued demand")
        self.renderer.finish()
        self.assertFalse(self.renderer._pending)
        self.assertFalse(self.renderer._running)
        self.assertIsNone(self.renderer._states[self.canvas].frame)
        self.canvas._zoom_animating = False
        self.draw()
        self.assertEqual(self.renderer._active_job.charts, self.sources)
