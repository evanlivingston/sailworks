"""Polar tab comparison controls and shared curve/scatter geometry."""

import math
import unittest
from pathlib import Path
from types import SimpleNamespace

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from gweatherrouting.core.datasource import NMEADataPacket
from gweatherrouting.core.nmeasession import NMEALog
from gweatherrouting.core.polarperformance import PerformanceSample
from gweatherrouting.core.utils import EventDispatcher
from gweatherrouting.gtk.polarperformance import PerformanceControls
from gweatherrouting.gtk.polarstack import PolarStack
from gweatherrouting.gtk.widgets.polar import PolarWidget
from test.test_polarperformance import gps, water, wind


def load_samples(controls, path, samples):
    session = controls.session
    session.finish_load(session.begin_load(), NMEALog(path, (), 0, False, samples))


class TestPerformanceDock(unittest.TestCase):
    def test_hide_float_redock_and_polar_change_preserve_performance(self):
        manager = EventDispatcher()
        manager.polars = ["first317.pol", "bavaria38.pol"]
        manager.get_metadata = lambda name: {}
        manager.get_path = lambda name: str(
            Path(__file__).resolve().parents[1] / "gweatherrouting/data/polars" / name
        )
        core = SimpleNamespace(
            polar_manager=manager, connectionManager=EventDispatcher()
        )
        window = Gtk.Window()
        window.set_default_size(1220, 850)
        self.addCleanup(window.destroy)
        stack = PolarStack(window, core, None)
        window.add(stack)
        window.show_all()
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        dock, controls = stack.tool_dock, stack.performance
        toggle = stack.builder.get_object("performance-toggle")
        self.assertTrue(dock.is_open("nmea-performance"))
        self.assertLessEqual(dock.notebook.get_allocated_width(), 340)
        self.assertGreater(stack.polarWidget.get_allocated_width(), 350)
        toolbar = stack.builder.get_object("polar-toolbar")
        self.assertEqual(toolbar.get_allocated_width(), stack.get_allocated_width())
        toolbar_bottom = toolbar.translate_coordinates(
            stack, 0, toolbar.get_allocated_height()
        )[1]
        self.assertGreaterEqual(
            dock.sidebar.translate_coordinates(stack, 0, 0)[1], toolbar_bottom
        )
        samples = (PerformanceSample(45, 10, 6, "stw"),)
        load_samples(controls, "race.log", samples)
        stack.nmea.live.set_active(False)
        dock.toggle_dock("nmea-performance")
        panel = dock.panels["nmea-performance"]
        self.assertTrue(panel.tool_floating)
        dock._hide_notebook(panel.get_parent())
        self.assertFalse(toggle.get_active())
        self.assertTrue(controls._alive)
        stack.load_polar("bavaria38.pol")
        self.assertFalse(dock.is_open("nmea-performance"))
        toggle.set_active(True)
        self.assertTrue(panel.tool_floating)
        self.assertEqual(controls.session.active_history.snapshot()[1], samples)
        self.assertEqual(stack.polarWidget.samples, samples)
        self.assertFalse(stack.nmea.live.get_active())
        dock.toggle_dock("nmea-performance")
        self.assertIs(panel.get_parent(), dock.notebook)
        toggle.set_active(False)
        core.connectionManager.dispatch(
            "data", [NMEADataPacket(s) for s in (water(), wind())]
        )
        self.assertEqual(controls.session.history.snapshot()[1], ())
        stack.nmea.source.set_active(0)
        stack.nmea.live.set_active(True)
        core.connectionManager.dispatch(
            "data", [NMEADataPacket(s) for s in (water(), wind())]
        )
        self.assertEqual(len(controls.session.history.snapshot()[1]), 1)
        self.assertFalse(dock.is_open("nmea-performance"))


class TestPerformanceControls(unittest.TestCase):
    def setUp(self):
        self.core = SimpleNamespace(
            polar_manager=None, connectionManager=EventDispatcher()
        )
        self.chart = PolarWidget(None, self.core, full_circle=True)
        self.chart.set_polar(
            SimpleNamespace(
                tws=[6, 10, 14],
                twa=[0, math.pi / 2, math.pi],
                speed_table=[[0, 0, 0], [4, 6, 8], [3, 4, 5]],
            )
        )
        self.controls = PerformanceControls(None, self.core, self.chart)
        self.controls.polar_changed()
        self.addCleanup(self.chart.destroy)
        self.addCleanup(self.controls.destroy)

    def test_basis_and_band_filters_do_not_mix_measurements(self):
        samples = (
            PerformanceSample(45, 10, 6, "stw"),
            PerformanceSample(315, 14, 7, "stw"),
            PerformanceSample(45, 10, 8, "sog"),
            PerformanceSample(45, 20, 8, "stw"),
        )
        load_samples(self.controls, "race.log", samples)
        self.assertEqual(self.chart.samples, samples[:2])
        self.controls.wind.set_active(2)  # 10 kt
        self.assertEqual(self.chart.samples, samples[:1])
        self.controls.basis.set_active(1)
        self.assertEqual(self.chart.samples, samples[2:3])
        self.controls.show_points.set_active(False)
        self.assertEqual(self.chart.samples, ())
        self.controls.show_points.set_active(True)
        self.controls._clear(None)
        self.assertEqual(self.chart.samples, ())

    def test_live_input_pause_and_source_switch(self):
        packets = [NMEADataPacket(s) for s in (water(), gps(), wind())]
        self.core.connectionManager.dispatch("data", packets)
        self.controls._tick()
        self.assertEqual(len(self.chart.samples), 1)
        self.controls.basis.set_active(1)
        self.assertEqual(self.chart.samples[0].basis, "sog")
        self.controls.session.set_streaming(False)
        self.controls._clear(None)
        self.core.connectionManager.dispatch("data", packets)
        self.controls._tick()
        self.assertEqual(self.chart.samples, ())
        load_samples(self.controls, "empty.log", ())
        self.assertIn("0 / 0", self.controls.count.get_text())

    def test_shared_scale_preserves_tacks_and_above_polar_points(self):
        self.chart.set_performance(
            (
                PerformanceSample(90, 10, 12, "stw"),
                PerformanceSample(270, 10, 12, "stw"),
                PerformanceSample(90, 10, 6, "stw"),
            )
        )
        allocation = Gdk.Rectangle()
        allocation.x, allocation.y = 0, 0
        allocation.width, allocation.height = 630, 630
        self.chart.show()
        self.chart.size_allocate(allocation)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 630, 630)
        self.chart.draw(self.chart, cairo.Context(surface))
        points = self.chart._point_positions
        self.assertEqual(len(points), 3)
        cx, cy = 315, 315
        self.assertGreater(points[0][0], cx)
        self.assertLess(points[1][0], cx)
        self.assertAlmostEqual(points[0][1], cy)
        self.assertAlmostEqual(points[0][0] - cx, cx - points[1][0])
        self.assertAlmostEqual(points[0][0] - cx, 2 * (points[2][0] - cx))

    def test_curve_hover_shows_wind_speed_and_angle_on_both_tacks(self):
        allocation = Gdk.Rectangle()
        allocation.width, allocation.height = 630, 630
        self.chart.show()
        self.chart.size_allocate(allocation)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 630, 630)
        self.chart.draw(self.chart, cairo.Context(surface))
        for x, y, angle in (
            (495, 315, "90.0"),
            (135, 315, "270.0"),
            (405, 375, "123.7"),
        ):
            with self.subTest(angle=angle):
                self.chart.on_mouse_move(self.chart, SimpleNamespace(x=x, y=y))
                self.assertEqual(self.chart.hover_text, f"TWS 10 kt · TWA {angle}°")
                self.assertEqual(self.chart.hover_wind, 10)
        self.chart.on_mouse_move(self.chart, SimpleNamespace(x=600, y=100))
        self.assertIsNone(self.chart.hover_text)
        self.assertIsNone(self.chart.hover_wind)
        self.chart.set_performance((), wind_filter=0)
        self.chart.draw(self.chart, cairo.Context(surface))
        self.chart.on_mouse_move(self.chart, SimpleNamespace(x=495, y=315))
        self.assertIsNone(self.chart.hover_text)

    def test_hover_immediately_dims_other_curves_and_leave_restores_them(self):
        allocation = Gdk.Rectangle()
        allocation.width, allocation.height = 630, 630
        self.chart.show()
        self.chart.size_allocate(allocation)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 630, 630)

        def red_at_other_curve():
            self.chart.draw(self.chart, cairo.Context(surface))
            surface.flush()
            return bytes(surface.get_data())[315 * surface.get_stride() + 435 * 4 + 2]

        original = red_at_other_curve()
        self.chart.on_mouse_move(self.chart, SimpleNamespace(x=495, y=315))
        self.assertEqual(self.chart.hover_text, "TWS 10 kt · TWA 90.0°")
        self.assertEqual(self.chart.hover_wind, 10)
        self.assertLess(red_at_other_curve(), original)
        self.chart.on_mouse_leave(self.chart, None)
        self.assertIsNone(self.chart.hover_text)
        self.assertIsNone(self.chart.hover_wind)
        self.assertEqual(red_at_other_curve(), original)
