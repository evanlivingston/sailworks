"""Progress delivery stays on GTK's main thread and ignores stale runs."""

import unittest
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
from weatherrouting import IsoPoint, RoutingNoWindError

from gweatherrouting.core.marine.chs import DownloadCancelledError

from gweatherrouting.core.marine.routing import distance_bearing, routing_completion
from gweatherrouting.gtk.chartstack_routing import ChartStackRouting


class ProgressHarness(ChartStackRouting):
    def __init__(self):
        self.progress_bar = Gtk.ProgressBar(show_text=True)
        self.currentRouting = SimpleNamespace(
            position=(0, 0),
            track=[(0, 0), (0, 1), (0, 2)],
            wp=1,
            end=False,
            log=[],
            _startingNewPoint=True,
        )
        routing_completion(self.currentRouting)
        self.stop_routing = False


class TestRoutingProgress(unittest.TestCase):
    def test_rejected_current_samples_draw_red_and_clear_with_isochrones(self):
        import cairo
        from gweatherrouting.gtk.maplayers.isochronesmaplayer import (
            IsochronesMapLayer,
            render_isochrones,
        )

        layer = IsochronesMapLayer()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 420, 240)
        cr = cairo.Context(surface)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        gpsmap = SimpleNamespace(
            convert_geographic_to_screen=lambda p: (
                p.get_degrees()[1] * 200,
                p.get_degrees()[0] * 200,
            )
        )
        sample = dict(latitude=0.5, longitude=0.5)
        layer.set_isochrones([], [], [sample])
        render_isochrones(gpsmap, cr, layer.frame, threading.Event())
        surface.flush()
        pixel = bytes(surface.get_data())[100 * surface.get_stride() + 100 * 4 :][:4]
        self.assertGreater(pixel[2], pixel[0])  # Cairo ARGB32 on little endian.
        layer.set_isochrones([], [])
        self.assertFalse(layer.excluded_current_samples)

    def test_isochrone_rendering_keeps_main_loop_responsive_and_clears_stale_frame(
        self,
    ):
        import cairo
        from test.map_helpers import MapCanvas
        from gweatherrouting.gtk.maplayers.isochronesmaplayer import IsochronesMapLayer

        canvas = MapCanvas()
        canvas.get_allocated_width = lambda: canvas.width
        canvas.get_allocated_height = lambda: canvas.height
        layer = IsochronesMapLayer()
        source = [[IsoPoint((49, -124)), IsoPoint((49.001, -124))]]
        layer.set_isochrones(source, [])
        source.clear()
        self.assertEqual(len(layer.frame.isochrones), 1)
        release, started = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        render = layer.renderer._render

        def slow_render(job):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            started.set()
            release.wait(2)
            return render(job)

        layer.renderer._render = slow_render
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas.width, canvas.height)
        layer.do_draw(canvas, cairo.Context(surface))
        self.assertTrue(started.wait(1))
        heartbeat = []
        GLib.idle_add(lambda: heartbeat.append(True) and False)
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        self.assertTrue(heartbeat)
        # Clearing a run cancels its in-flight frame rather than repainting it.
        layer.set_isochrones([], [])
        layer.do_draw(canvas, cairo.Context(surface))
        release.set()
        deadline = time.monotonic() + 3
        while layer.renderer._running and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        self.assertFalse(layer.renderer._running)
        self.assertNotIn(canvas, layer.renderer._states)

    def test_batched_isochrone_joins_preserve_original_geodesic_cutoff(self):
        from weatherrouting import utils
        from gweatherrouting.gtk.maplayers.isochronesmaplayer import (
            IsochroneFrame,
            render_isochrones,
        )

        points = [
            IsoPoint(p)
            for p in ((49, -124), (49.001, -124), (50, -123), (50.001, -123))
        ]
        view = SimpleNamespace(
            convert_geographic_to_screen_batch=lambda lat, lon: (lon, lat)
        )
        cr = Mock()
        render_isochrones(
            view, cr, IsochroneFrame((tuple(points),), (), ()), threading.Event()
        )
        expected = [
            b.pos
            for a, b in zip(points, points[1:])
            if utils.ortodromic(*a.pos, *b.pos)[0] <= 3
        ]
        self.assertEqual(
            [c.args for c in cr.line_to.call_args_list],
            [(p[1], p[0]) for p in expected],
        )

    def test_preflight_response_does_not_deadlock_with_application_gdk_lock(self):
        # Use a fresh process: enabling GDK's process-global lock would affect
        # other native tests. This reproduces MainWindow's actual GTK setup.
        import subprocess
        import sys
        import textwrap

        program = textwrap.dedent("""
            import threading
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gdk, GLib, Gtk
            from types import SimpleNamespace
            from gweatherrouting.gtk.chartstack_routing import ChartStackRouting
            Gdk.threads_init()
            harness = ChartStackRouting.__new__(ChartStackRouting)
            harness.parent = Gtk.Window()
            harness.parent.show_all()
            harness._auto_currents = True
            report = SimpleNamespace(blocked=False, describe=lambda auto: "Coverage warning")
            constructor = Gtk.MessageDialog
            def dialog(*args, **kwargs):
                widget = constructor(*args, **kwargs)
                def click():
                    widget.get_widget_for_response(Gtk.ResponseType.ACCEPT).clicked()
                    return False
                GLib.timeout_add(30, click)
                return widget
            Gtk.MessageDialog = dialog
            def worker():
                assert harness._routing_ui(harness._show_current_preflight, report)
                harness._routing_ui(lambda: print("POST_DIALOG_UI_OK", flush=True))
                GLib.idle_add(Gtk.main_quit)
            GLib.timeout_add(30, lambda: threading.Thread(target=worker, daemon=True).start())
            Gtk.main()
            harness.parent.destroy()
        """)
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("POST_DIALOG_UI_OK", result.stdout)

    def test_preflight_dialog_runs_on_main_thread_and_cancel_prevents_search(self):
        from test.test_current_preflight import routing
        from test.test_current_routing import current_field

        constructor = Gtk.MessageDialog
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                harness = self.thread_harness()
                harness.currentRouting = routing(current_field(masked=blocked))
                harness.currentRouting.grib = Mock(
                    get_wind_at=Mock(return_value=(0, 10))
                )
                harness.currentRouting.step = Mock()
                shown = []

                def dialog(*args, **kwargs):
                    self.assertIs(threading.current_thread(), threading.main_thread())
                    widget = constructor(*args, **kwargs)
                    shown.append(widget)

                    def respond():
                        self.assertEqual(
                            widget.get_widget_for_response(Gtk.ResponseType.ACCEPT)
                            is None,
                            blocked,
                        )
                        widget.response(Gtk.ResponseType.CANCEL)
                        return False

                    GLib.timeout_add(20, respond)
                    return widget

                with patch(
                    "gweatherrouting.gtk.chartstack_routing.Gtk.MessageDialog", dialog
                ):
                    self.run_worker(harness.on_routing_step)
                self.assertEqual(len(shown), 1)
                harness.currentRouting.step.assert_not_called()
                harness.core.routingManager.append.assert_not_called()
                self.assertEqual(harness.progress_bar.get_text(), "Routing not started")

    def test_preflight_warning_can_be_accepted_before_search(self):
        from test.test_current_preflight import routing
        from test.test_current_routing import current_field

        harness = self.thread_harness()
        harness.currentRouting = routing(current_field())
        harness.currentRouting.end = False
        harness.currentRouting.log = []
        harness.currentRouting._startingNewPoint = True
        harness.currentRouting.grib = Mock(get_wind_at=Mock(return_value=(0, 10)))
        harness.currentRouting.step = Mock(side_effect=DownloadCancelledError())
        constructor = Gtk.MessageDialog

        def dialog(*args, **kwargs):
            widget = constructor(*args, **kwargs)
            GLib.timeout_add(20, lambda: widget.response(Gtk.ResponseType.ACCEPT))
            return widget

        with patch("gweatherrouting.gtk.chartstack_routing.Gtk.MessageDialog", dialog):
            self.run_worker(harness.on_routing_step)
        harness.currentRouting.step.assert_called_once()

    def run_worker(self, target):
        errors = []

        def work():
            try:
                target()
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        context = GLib.MainContext.default()
        while worker.is_alive() and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        self.assertFalse(worker.is_alive(), "Routing worker did not finish")
        self.assertEqual(errors, [])

    def thread_harness(self):
        harness = ProgressHarness()
        harness.parent = Gtk.Window()
        self.addCleanup(harness.parent.destroy)

        def on_main(*args, **kwargs):
            self.assertIs(threading.current_thread(), threading.main_thread())

        harness.map = SimpleNamespace(queue_draw=on_main)
        harness.isochronesMapLayer = SimpleNamespace(set_isochrones=on_main)
        harness.time_control = SimpleNamespace(set_time=on_main)
        stop = SimpleNamespace(hide=on_main)
        harness.builder = SimpleNamespace(get_object=lambda *_: stop)
        harness.update_routings = Mock(side_effect=on_main)
        harness.core = SimpleNamespace(
            routingManager=SimpleNamespace(
                append=Mock(side_effect=on_main), get_unique_name=lambda name: name
            ),
            marine_manager=SimpleNamespace(fields=SimpleNamespace(add=on_main)),
        )
        harness.currentRouting.grib = Mock(get_wind_at=Mock(return_value=(0, 10)))
        harness.currentRouting.current_environment = None
        harness.currentRouting.name = "test-routing"
        harness.currentRouting.time = datetime(2026, 10, 11, 12)
        return harness

    def test_real_error_dialog_is_created_and_run_on_main_thread(self):
        constructor = Gtk.MessageDialog
        for failure in (None, ValueError("Missing current data"), RoutingNoWindError()):
            with self.subTest(failure=failure):
                harness = self.thread_harness()
                if failure is None:
                    harness.currentRouting.grib.get_wind_at.return_value = None
                else:
                    harness.currentRouting.step = Mock(side_effect=failure)
                shown = []

                def dialog(*args, **kwargs):
                    self.assertIs(threading.current_thread(), threading.main_thread())
                    widget = constructor(*args, **kwargs)
                    shown.append(widget)
                    # Actually realize the native window through dialog.run().
                    GLib.timeout_add(20, lambda: widget.response(Gtk.ResponseType.OK))
                    return widget

                with patch(
                    "gweatherrouting.gtk.chartstack_routing.Gtk.MessageDialog", dialog
                ):
                    self.run_worker(harness.on_routing_step)
                self.assertEqual(len(shown), 1)
                self.assertFalse(harness.progress_bar.get_visible())
                harness.core.routingManager.append.assert_not_called()

    def test_success_and_cancellation_update_widgets_and_save_on_main_thread(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                harness = self.thread_harness()

                def step():
                    self.assertIsNot(
                        threading.current_thread(), threading.main_thread()
                    )
                    if cancel:
                        raise DownloadCancelledError()
                    harness.currentRouting.end = True
                    return SimpleNamespace(
                        time=harness.currentRouting.time,
                        isochrones=[],
                        path=[IsoPoint((0, 1), time=harness.currentRouting.time)],
                    )

                harness.currentRouting.step = step
                self.run_worker(harness.on_routing_step)
                self.assertEqual(
                    harness.core.routingManager.append.call_count, 0 if cancel else 1
                )
                self.assertEqual(
                    harness.progress_bar.get_text(),
                    "Routing cancelled" if cancel else "100%",
                )

    def test_ui_dispatch_propagates_callback_errors_and_closing_releases_worker(self):
        harness = self.thread_harness()

        def worker():
            with self.assertRaisesRegex(ValueError, "UI callback failed"):
                harness._routing_ui(Mock(side_effect=ValueError("UI callback failed")))

        self.run_worker(worker)
        with patch("gweatherrouting.gtk.chartstack_routing.GLib.idle_add"):
            waiting = threading.Event()
            errors = []

            def closing_worker():
                waiting.set()
                try:
                    with self.assertRaises(DownloadCancelledError):
                        harness._routing_ui(lambda: None)
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=closing_worker, daemon=True)
            thread.start()
            self.assertTrue(waiting.wait(1))
            harness._close_routing_ui()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_comparison_scenarios_save_and_finish_on_main_thread(self):
        harness = self.thread_harness()
        template = vars(harness.currentRouting).copy()

        def create(*args, **kwargs):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            routing = SimpleNamespace(**template)

            def step():
                routing.end = True
                return SimpleNamespace(
                    time=routing.time,
                    isochrones=[],
                    path=[IsoPoint((0, 1), time=routing.time)],
                )

            routing.step = step
            return routing

        harness.core.create_routing = create
        scenario = dict(
            time_offset_hours=0,
            wind_speed_pct=0,
            wind_dir_offset=0,
            polar_efficiency_pct=100,
        )
        harness._comparison_params = dict(
            scenarios=[scenario, scenario],
            algorithm=None,
            polar_file="unused",
            polar_name="test",
            track_or_poi=None,
            start_datetime=template["time"],
            start_position=None,
            validity_providers=[],
            disable_coastline=False,
            use_currents=False,
        )
        self.run_worker(harness.on_comparison_routing_step)
        self.assertEqual(harness.core.routingManager.append.call_count, 2)
        self.assertEqual(harness.progress_bar.get_text(), "2/2 scenarios complete")
        self.assertEqual(harness.progress_bar.get_fraction(), 1)

    def test_download_notifications_are_dispatched_to_main_thread(self):
        harness = self.thread_harness()

        def step(routing, manager, auto, report, save):
            report("Downloading missing currents")
            save([object()])

        with patch("gweatherrouting.gtk.chartstack_routing.step_with_coverage", step):
            self.run_worker(harness._step_with_currents)
        self.assertEqual(
            harness.progress_bar.get_text(), "Downloading missing currents"
        )

    def test_chart_progress_bar_enables_percentage_text(self):
        from gweatherrouting.common import resource_path

        builder = Gtk.Builder.new_from_file(
            resource_path("gweatherrouting", "gtk/chartstack.glade")
        )
        self.assertTrue(builder.get_object("progressbar").get_show_text())

    def test_updates_are_coalesced_before_main_thread_delivery(self):
        harness = ProgressHarness()
        leg = distance_bearing((0, 0), (0, 1))[0]
        with patch("gweatherrouting.gtk.chartstack_routing.GLib.idle_add") as idle:
            harness._report_search_progress(leg, 0, 4)
            harness._last_search_progress = 0
            harness._report_search_progress(leg / 2, 2, 4)
            self.assertEqual(idle.call_count, 1)
            self.assertIsNone(harness.progress_bar.get_text())
            idle.call_args.args[0]()
        self.assertAlmostEqual(harness.progress_bar.get_fraction(), 0.25)
        self.assertIn("≈25%", harness.progress_bar.get_text())
        self.assertIn("checking paths 2/4", harness.progress_bar.get_text())
        self.assertTrue(harness.progress_bar.get_visible())

    def test_late_updates_do_not_replace_completion_cancellation_or_another_run(self):
        for state in ("complete", "cancelled", "replaced", "cleared"):
            with self.subTest(state=state):
                harness = ProgressHarness()
                with patch("gweatherrouting.gtk.chartstack_routing.GLib.idle_add"):
                    harness._report_search_progress(100, 2, 4)
                harness.progress_bar.set_text("Finished")
                if state == "complete":
                    harness.currentRouting.end = True
                elif state == "cancelled":
                    harness.stop_routing = True
                elif state == "cleared":
                    harness._pending_search_progress = None
                else:
                    harness.currentRouting = None
                harness._apply_search_progress()
                self.assertEqual(harness.progress_bar.get_text(), "Finished")
