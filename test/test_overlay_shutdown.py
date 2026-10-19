"""Terminal cleanup must not depend on a still-running GUI event loop."""

from threading import Event, get_ident
import unittest

import cairo

from gweatherrouting.rendering.overlayrenderer import OverlayRenderer
from test.test_overlay_scheduling import Canvas


class Renderer(OverlayRenderer):
    def __init__(self):
        self.callbacks = []
        self.scheduled = Event()
        self.entered = Event()
        self.resume = Event()
        self.released = Event()
        self.release_threads = []
        super().__init__(self.schedule_callback)

    def schedule_callback(self, callback, *args):
        self.callbacks.append((callback, args))
        self.scheduled.set()

    def _render(self, job):
        self.entered.set()
        if not self.resume.wait(5):
            raise RuntimeError("Test did not release the worker")
        return None

    def _release_resources(self):
        self.release_threads.append(get_ident())
        self.released.set()


class TestOverlayShutdown(unittest.TestCase):
    def setUp(self):
        self.renderer = Renderer()
        self.addCleanup(self.renderer.close)
        self.addCleanup(self.renderer.resume.set)
        self.canvas = Canvas()
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 240)

    def start(self):
        self.renderer.draw(
            self.canvas, cairo.Context(self.surface), (("chart",),), "palette"
        )
        self.assertTrue(self.renderer.entered.wait(2))

    def test_close_during_worker_releases_without_scheduling_completion(self):
        self.start()
        job = self.renderer._active_job
        self.renderer.close()
        self.assertTrue(job.cancel.is_set())
        self.assertFalse(self.renderer.released.is_set())
        self.renderer.resume.set()
        self.assertTrue(self.renderer.released.wait(2))
        self.assertNotEqual(self.renderer.release_threads, [get_ident()])
        self.assertFalse(self.renderer.callbacks)

    def test_close_after_worker_before_ui_callback_releases_once(self):
        self.start()
        self.renderer.resume.set()
        self.assertTrue(self.renderer.scheduled.wait(2))
        self.renderer.close()
        self.assertEqual(self.renderer.release_threads, [get_ident()])
        for callback, args in self.renderer.callbacks:
            callback(*args)
        self.assertFalse(self.renderer._running)
        self.assertFalse(self.renderer._states)
        self.renderer.close()
        self.assertEqual(self.renderer.release_threads, [get_ident()])

    def test_idle_close_prevents_new_visible_and_speculative_jobs(self):
        self.renderer.close()
        self.renderer.close()
        self.renderer.draw(
            self.canvas, cairo.Context(self.surface), (("chart",),), "palette"
        )
        self.assertFalse(
            self.renderer.prefetch(self.canvas, (), None, (), 1, "palette")
        )
        self.assertFalse(self.renderer.entered.is_set())
        self.assertFalse(self.renderer._states)
        self.assertEqual(self.renderer.release_threads, [get_ident()])
