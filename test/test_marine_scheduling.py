"""Marine time-only completion policy on a manually driven overlay worker."""

from datetime import timedelta
from types import SimpleNamespace
import unittest

import cairo

from gweatherrouting.core.marine.manager import Selection
from gweatherrouting.core.marine.models import Capability
from gweatherrouting.rendering.marinemaplayer import MarineRenderer, MarineSources
from gweatherrouting.rendering.overlayrenderer import OverlayRenderer
from test.test_marine import START
from test.test_marine_fields import field
from test.test_overlay_scheduling import Canvas, Renderer


class ManualMarineRenderer(MarineRenderer):
    # Reuse the existing deterministic overlay worker, not its weather policy.
    _start_next = Renderer._start_next
    _schedule_event = Renderer._schedule_event
    finish = Renderer.finish

    def __init__(self):
        super().__init__(lambda *args: None)
        self.started = []
        self.events = []


class TestMarineScheduling(unittest.TestCase):
    def setUp(self):
        self.canvas = Canvas()
        self.renderer = ManualMarineRenderer()
        self.sources = MarineSources(
            (field(),),
            Selection((), Capability.WATER_LEVEL),
            Selection((), Capability.STATION_CURRENT),
        )
        self.cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 240))

    def draw(self, instant=START):
        self.renderer.draw(self.canvas, self.cr, ((self.sources,),), instant)

    def test_generic_overlay_remains_gated_during_zoom(self):
        renderer = Renderer()
        self.canvas._zoom_animating = True
        OverlayRenderer.draw(renderer, self.canvas, self.cr, (("source",),), START)
        self.assertFalse(renderer.started)
        self.assertFalse(renderer._pending)

    def test_marine_requests_during_zoom(self):
        self.canvas._zoom_animating = True
        self.draw()
        self.assertEqual(len(self.renderer.started), 1)
        self.assertEqual(self.renderer._active_job.viewport._zoom, self.canvas._zoom)

    def test_zoom_only_work_finishes_and_coalesces_latest_view(self):
        self.draw()
        running = self.renderer._active_job
        self.canvas._zoom_animating = True
        previous = None
        for zoom in (8.1, 8.2, 8.3):
            self.canvas._zoom = zoom
            self.draw()
            self.assertFalse(running.cancel.is_set())
            self.assertEqual(len(self.renderer.started), 1)
            self.assertEqual(len(self.renderer._pending), 1)
            if previous is not None:
                self.assertTrue(previous.cancel.is_set())
            previous = self.renderer._pending[self.canvas]
        latest = previous
        self.draw()
        self.assertIs(self.renderer._pending[self.canvas], latest)
        self.renderer.finish()
        state = self.renderer._states[self.canvas]
        self.assertIs(state.frame[0], running)
        self.assertIs(self.renderer._active_job, latest)
        self.renderer.finish()
        self.assertIs(state.frame[0], latest)
        self.assertTrue(self.renderer._fits_job(latest, self.canvas))
        self.draw()
        self.assertEqual(len(self.renderer.started), 2)

    def test_zoom_incompatibility_cancels_detached_active_work(self):
        for change in (
            "source",
            "factor",
            "pan",
            "latitude",
            "size",
            "expired",
            "settled",
        ):
            with self.subTest(change=change):
                self.setUp()
                self.draw()
                running = self.renderer._active_job
                self.canvas._zoom_animating = True
                self.canvas._zoom += 0.1
                self.draw()
                queued = self.renderer._pending[self.canvas]
                target = START
                if change == "source":
                    self.sources = MarineSources(
                        (), self.sources.levels, self.sources.currents
                    )
                elif change == "factor":
                    self.canvas.factor = 2
                elif change == "pan":
                    self.canvas.pan(1)
                elif change == "latitude":
                    self.canvas._center_lat += 0.01
                elif change == "size":
                    self.canvas.width += 1
                elif change == "expired":
                    target += timedelta(days=1)
                else:
                    self.canvas._zoom_animating = False
                self.draw(target)
                self.assertTrue(running.cancel.is_set())
                self.assertTrue(
                    queued.cancel.is_set()
                    or queued is self.renderer._states[self.canvas].requested
                )
                self.renderer.finish()
                self.assertIsNone(self.renderer._states[self.canvas].frame)

    def test_cursor_zoom_survives_multiple_ticks_and_publishes_before_settle(self):
        self.canvas._zoom_animating = True
        self.draw()
        running = self.renderer._active_job
        self.draw()  # Same zoom as active work must not revoke its grace.
        previous = None
        for zoom, latitude, longitude in (
            (8.1, 0.01, 0.02),
            (8.2, 0.02, 0.04),
            (8.0, 0.0, 0.0),  # Returning to active scale is also compatible.
            (8.3, 0.03, 0.06),
        ):
            self.canvas._zoom = zoom
            self.canvas._center_lat = latitude
            self.canvas._center_lon = longitude
            self.draw()
            self.draw()  # Redraw without a camera tick.
            self.assertFalse(running.cancel.is_set())
            self.assertEqual(len(self.renderer.started), 1)
            self.assertLessEqual(len(self.renderer._pending), 1)
            if previous is not None:
                self.assertTrue(previous.cancel.is_set())
            previous = self.renderer._pending.get(self.canvas)

        latest = previous
        self.renderer.finish()
        state = self.renderer._states[self.canvas]
        self.assertIs(state.frame[0], running)
        self.assertIsNotNone(running.published_at)
        self.assertTrue(self.renderer._can_preview(running, state.key))
        self.assertIs(self.renderer._active_job, latest)
        self.canvas._zoom = 8.4
        self.canvas._center_lat = 0.04
        self.canvas._center_lon = 0.08
        self.draw()
        self.assertFalse(latest.cancel.is_set())
        self.canvas._zoom_animating = False
        self.draw()
        self.assertTrue(latest.cancel.is_set())
        final = self.renderer._pending[self.canvas]
        self.renderer.finish()  # Cancelled intermediate must not publish.
        self.assertIs(state.frame[0], running)
        self.assertIs(self.renderer._active_job, final)
        self.renderer.finish()
        self.assertIs(state.frame[0], final)
        self.assertTrue(self.renderer._fits_job(final, self.canvas))
        self.assertFalse(self.renderer._pending)

    def test_cursor_zoom_still_invalidates_pan_and_resize(self):
        for change in ("longitude", "latitude", "width", "height"):
            with self.subTest(change=change):
                self.setUp()
                self.canvas._zoom_animating = True
                self.draw()
                running = self.renderer._active_job
                self.canvas._zoom += 0.1
                self.canvas._center_lat += 0.01
                self.canvas._center_lon += 0.02
                self.draw()
                queued = self.renderer._pending[self.canvas]
                if change == "longitude":
                    self.canvas._center_lon += 0.001
                elif change == "latitude":
                    self.canvas._center_lat += 0.001
                else:
                    setattr(self.canvas, change, getattr(self.canvas, change) + 1)
                self.draw()
                self.assertTrue(running.cancel.is_set())
                if change in ("width", "height"):
                    self.assertTrue(queued.cancel.is_set())
                else:
                    self.assertIs(self.renderer._pending[self.canvas], queued)
                self.renderer.finish()
                self.assertIsNone(self.renderer._states[self.canvas].frame)
                self.assertLessEqual(len(self.renderer._pending), 1)

    def test_time_only_work_publishes_while_latest_request_remains_queued(self):
        self.draw()
        running = self.renderer._active_job
        for minutes in (5, 10, 15):
            self.draw(START + timedelta(minutes=minutes))
            self.assertFalse(running.cancel.is_set())
            self.assertEqual(len(self.renderer._pending), 1)
        latest = self.renderer._pending[self.canvas]
        self.draw(latest.palette)
        self.assertIs(self.renderer._pending[self.canvas], latest)
        self.assertEqual(latest.palette, START + timedelta(minutes=15))
        self.renderer.finish()
        state = self.renderer._states[self.canvas]
        self.assertIs(state.frame[0], running)
        self.assertNotEqual(running.key, state.key)
        self.assertTrue(self.renderer._can_preview(running, state.key))
        self.assertIs(state.requested, latest)
        self.assertIs(self.renderer._active_job, latest)
        self.renderer.finish()
        self.assertIs(state.frame[0], latest)
        self.assertEqual(latest.key, state.key)

    def test_incompatible_demand_cancels_even_detached_active_work(self):
        for change in ("source", "factor", "pan", "zoom", "size", "expired"):
            with self.subTest(change=change):
                self.setUp()
                self.draw()
                running = self.renderer._active_job
                target = START + timedelta(minutes=15)
                self.draw(target)
                self.assertIsNot(self.renderer._states[self.canvas].requested, running)
                if change == "source":
                    self.sources = MarineSources(
                        (), self.sources.levels, self.sources.currents
                    )
                elif change == "factor":
                    self.canvas.factor = 2
                elif change == "pan":
                    self.canvas.pan(160)
                elif change == "zoom":
                    self.canvas._zoom += 1
                elif change == "size":
                    self.canvas.width += 1
                else:
                    target = START + timedelta(days=1)
                self.draw(target)
                self.assertTrue(running.cancel.is_set())
                self.renderer.finish()
                self.assertIsNone(self.renderer._states[self.canvas].frame)

    def test_station_only_completion_uses_enabled_coverage(self):
        for capability in (Capability.WATER_LEVEL, Capability.STATION_CURRENT):
            with self.subTest(capability=capability):
                self.setUp()
                selection = Selection(
                    (
                        SimpleNamespace(
                            requested_start=START,
                            requested_end=START + timedelta(hours=1),
                        ),
                    ),
                    capability,
                )
                self.sources = MarineSources(
                    (),
                    (
                        selection
                        if capability == Capability.WATER_LEVEL
                        else self.sources.levels
                    ),
                    (
                        selection
                        if capability == Capability.STATION_CURRENT
                        else self.sources.currents
                    ),
                )
                self.draw()
                running = self.renderer._active_job
                self.assertTrue(self.renderer._can_complete(running, running.key))
                for minutes, expected in (
                    (-1, False),
                    (15, True),
                    (60, True),
                    (61, False),
                ):
                    key = (START + timedelta(minutes=minutes), *running.key[1:])
                    self.assertEqual(
                        self.renderer._can_complete(running, key), expected
                    )
