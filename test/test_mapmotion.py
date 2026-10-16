"""Map motion, pointer anchoring, and live Display preferences."""

import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gweatherrouting.gtk.widgets.mapmotion import (
    DragVelocity,
    PanInertia,
    ZoomAnimation,
)
from gweatherrouting.gtk.widgets.mapwidget import Gdk, Gtk, MapPoint, MapWidget
from test.map_helpers import MapCanvas as TestMap


class MotionMap(TestMap):
    MIN_ZOOM, MAX_ZOOM = 1, 20

    def __init__(self):
        super().__init__()
        self._smooth_zoom = self._pan_inertia_enabled = True
        self._zoom_animation = self._inertia = self._last_frame_time = None
        self._motion_tick = 0
        self._motion_destroyed = self._display_bound = self._zoom_animating = False
        self._dragging = False
        self._pinch = None
        self._trackpad_scroll_pans = True
        self._layers = []
        self._drag_velocity = DragVelocity()
        self.add_tick_callback = Mock(return_value=1)
        self.remove_tick_callback = Mock()


for name in (
    "_request_zoom",
    "_apply_zoom",
    "_cancel_motion",
    "_ensure_motion_tick",
    "_animate_motion",
    "_move_pixels",
    "_set_smooth_zoom",
    "_set_pan_inertia",
    "_destroy_motion",
    "bind_display_settings",
    "_on_button_press",
    "_on_motion",
    "_on_button_release",
    "_on_pinch_begin",
    "_on_pinch_scale",
    "_on_pinch_end",
    "_on_scroll",
    "_pan",
):
    setattr(MotionMap, name, getattr(MapWidget, name))


class TestMotion(unittest.TestCase):
    def test_pinch_is_cumulative_anchored_and_cancels_motion(self):
        canvas = MotionMap()
        pointer = (83, 71)
        anchor = canvas.convert_screen_to_geographic(*pointer)
        canvas._request_zoom(1, *pointer)
        gesture = Mock()
        gesture.get_bounding_box_center.return_value = (True, *pointer)
        canvas._on_pinch_begin(gesture, None)
        self.assertIsNone(canvas._zoom_animation)
        for scale, expected in ((2, 13), (2, 13), (0.5, 11), (1, 12)):
            canvas._on_pinch_scale(gesture, scale)
            self.assertEqual(canvas._zoom, expected)
            actual = canvas.convert_geographic_to_screen(anchor)
            self.assertAlmostEqual(actual[0], pointer[0], places=7)
            self.assertAlmostEqual(actual[1], pointer[1], places=7)
        canvas._on_pinch_end(gesture, None)
        self.assertFalse(canvas._zoom_animating)
        canvas._on_pinch_scale(gesture, 4)
        self.assertEqual(canvas._zoom, 12)

    def test_pinch_limits_invalid_scales_and_unmap(self):
        canvas = MotionMap()
        gesture = Mock()
        gesture.get_bounding_box_center.return_value = (False, 0, 0)
        canvas._on_pinch_begin(gesture, None)
        for scale in (0, -1, float("nan"), float("inf")):
            canvas._on_pinch_scale(gesture, scale)
            self.assertEqual(canvas._zoom, 12)
        canvas._on_pinch_scale(gesture, 2**30)
        self.assertEqual(canvas._zoom, 20)
        canvas._on_pinch_scale(gesture, 2**-30)
        self.assertEqual(canvas._zoom, 1)
        canvas._cancel_motion()
        canvas._on_pinch_scale(gesture, 1)
        self.assertEqual(canvas._zoom, 1)
        self.assertFalse(canvas._zoom_animating)

    def scroll_event(self, direction=Gdk.ScrollDirection.SMOOTH, **kwargs):
        values = dict(
            direction=direction,
            x=83,
            y=71,
            state=0,
            get_scroll_deltas=lambda: (True, 20, -30),
            get_pointer_emulated=lambda: False,
        )
        values.update(kwargs)
        return SimpleNamespace(**values)

    def test_mac_trackpad_pans_both_axes_without_extra_inertia(self):
        canvas = MotionMap()
        before = canvas.convert_screen_to_geographic(200, 150)
        canvas._on_scroll(canvas, self.scroll_event())
        after = canvas.convert_geographic_to_screen(before)
        self.assertAlmostEqual(after[0], 180)
        self.assertAlmostEqual(after[1], 180)
        self.assertEqual(canvas._zoom, 12)
        self.assertIsNone(canvas._inertia)
        self.assertEqual(canvas._motion_tick, 0)

    def test_mac_emulated_scroll_is_ignored_and_wheel_still_zooms(self):
        canvas = MotionMap()
        canvas._on_scroll(
            canvas,
            self.scroll_event(
                Gdk.ScrollDirection.UP,
                get_pointer_emulated=lambda: True,
            ),
        )
        self.assertIsNone(canvas._zoom_animation)
        canvas._on_scroll(canvas, self.scroll_event(Gdk.ScrollDirection.UP))
        self.assertEqual(canvas._zoom_animation.target, 13)

    def test_modifier_scroll_zooms_and_other_platforms_keep_scroll_zoom(self):
        canvas = MotionMap()
        for modifier in (
            Gdk.ModifierType.CONTROL_MASK,
            Gdk.ModifierType.META_MASK,
            Gdk.ModifierType.MOD2_MASK,
        ):
            canvas._on_scroll(canvas, self.scroll_event(state=modifier))
            self.assertAlmostEqual(canvas._zoom_animation.target, 12.3)
            canvas._cancel_motion()
        canvas._trackpad_scroll_pans = False
        canvas._on_scroll(canvas, self.scroll_event())
        self.assertEqual(canvas._zoom_animation.target, 14)

    def test_scroll_does_not_interrupt_pinch(self):
        canvas = MotionMap()
        gesture = Mock()
        gesture.get_bounding_box_center.return_value = (True, 83, 71)
        canvas._on_pinch_begin(gesture, None)
        canvas._on_scroll(canvas, self.scroll_event())
        canvas._on_pinch_scale(gesture, 2)
        self.assertEqual(canvas._zoom, 13)

    def test_native_gesture_signals_are_connected(self):
        canvas = MapWidget()
        try:
            self.assertEqual(
                canvas._zoom_gesture.get_propagation_phase(),
                Gtk.PropagationPhase.CAPTURE,
            )
            # GTK requires a real event sequence for querying the gesture
            # center. Supply that center while exercising signal connections.
            with patch.object(
                canvas._zoom_gesture,
                "get_bounding_box_center",
                return_value=(True, 0, 0),
            ):
                canvas._zoom_gesture.emit("begin", None)
            canvas._zoom_gesture.emit("scale-changed", 2.0)
            self.assertEqual(canvas._zoom, 4)
            canvas._zoom_gesture.emit("cancel", None)
            self.assertIsNone(canvas._pinch)
            self.assertFalse(canvas._zoom_animating)
        finally:
            canvas.destroy()

    def test_zoom_eases_monotonically_and_finishes_exactly(self):
        for start, target in ((10, 12), (12, 10)):
            animation = ZoomAnimation(start, target, 1, (0, 0), (0, 0))
            values = [animation.value_at(1 + i / 100)[0] for i in range(31)]
            self.assertEqual(values[0], start)
            self.assertEqual(values[-1], target)
            self.assertTrue(
                all(
                    min(start, target) <= value <= max(start, target)
                    for value in values
                )
            )
            self.assertTrue(animation.value_at(1.21)[1])
            self.assertGreater(abs(values[10] - start), abs(target - start) / 2)

    def test_inertia_is_independent_of_frame_rate_and_stops(self):
        distances = []
        for fps in (30, 60, 120):
            inertia = PanInertia(600, -300)
            dx = dy = 0
            for _ in range(fps):
                x, y = inertia.step(1 / fps)
                dx += x
                dy += y
            self.assertFalse(inertia.active)
            distances.append(dx)
            self.assertAlmostEqual(dx, -2 * dy)
        self.assertAlmostEqual(distances[0], distances[2], places=8)
        self.assertLess(distances[0], 96)

    def test_paused_release_and_tiny_drags_do_not_coast(self):
        for points, release in (
            ([(0, 0), (0.03, 30)], 0.2),
            ([(0, 0), (0.03, 1)], 0.04),
        ):
            velocity = DragVelocity()
            for now, x in points:
                velocity.add(now, x, 0)
            self.assertEqual(velocity.release(release, points[-1][1], 0), (0, 0))

    def test_recent_flick_velocity_is_capped(self):
        velocity = DragVelocity()
        velocity.add(0, 0, 0)
        velocity.add(1, 0, 0)
        velocity.add(1.02, 100, 100)
        vx, vy = velocity.release(1.021, 100, 100)
        self.assertAlmostEqual(math.hypot(vx, vy), 1400)
        self.assertAlmostEqual(vx, vy)

    def test_zoom_keeps_cursor_anchor_on_every_frame(self):
        canvas = MotionMap()
        pointer = (83, 71)
        anchor = canvas.convert_screen_to_geographic(*pointer)
        canvas._request_zoom(1, *pointer)
        start = canvas._zoom_animation.started_at
        for offset in (0.03, 0.08, 0.15, 0.25):
            frame = SimpleNamespace(
                get_frame_time=lambda: int((start + offset) * 1000000)
            )
            canvas._animate_motion(canvas, frame)
            actual = canvas.convert_geographic_to_screen(anchor)
            self.assertAlmostEqual(actual[0], pointer[0], places=7)
            self.assertAlmostEqual(actual[1], pointer[1], places=7)
        self.assertEqual(canvas._zoom, 13)
        self.assertFalse(canvas._zoom_animating)
        self.assertEqual(canvas._motion_tick, 0)

    def test_repeated_input_accumulates_target_and_obeys_limits(self):
        canvas = MotionMap()
        canvas._request_zoom(1, 200, 150)
        canvas._request_zoom(1, 200, 150)
        self.assertEqual(canvas._zoom_animation.target, 14)
        canvas._request_zoom(100, 200, 150)
        self.assertEqual(canvas._zoom_animation.target, 20)
        canvas._request_zoom(-100, 200, 150)
        self.assertEqual(canvas._zoom_animation.target, 1)

    def test_disabling_zoom_settles_and_click_stops_inertia(self):
        canvas = MotionMap()
        canvas._request_zoom(1, 83, 71)
        canvas._set_smooth_zoom(False)
        self.assertEqual(canvas._zoom, 13)
        self.assertIsNone(canvas._zoom_animation)
        canvas._request_zoom(1, 83, 71)
        self.assertEqual(canvas._zoom, 14)
        self.assertEqual(canvas._motion_tick, 0)
        canvas._inertia = PanInertia(300, 0)
        canvas._ensure_motion_tick()
        canvas._on_button_press(canvas, SimpleNamespace(button=1, x=100, y=100))
        self.assertIsNone(canvas._inertia)
        self.assertTrue(canvas._dragging)

    def test_drag_release_coasts_in_drag_direction(self):
        canvas = MotionMap()
        with patch(
            "gweatherrouting.gtk.widgets.mapwidget.GLib.get_monotonic_time"
        ) as clock:
            clock.return_value = 1000000
            canvas._on_button_press(canvas, SimpleNamespace(button=1, x=100, y=100))
            clock.return_value = 1040000
            canvas._on_motion(canvas, SimpleNamespace(x=120, y=110))
            clock.return_value = 1041000
            canvas._on_button_release(canvas, SimpleNamespace(button=1, x=120, y=110))
        before = canvas.convert_geographic_to_screen(MapPoint(47.6, -122.4))
        canvas._animate_motion(canvas, SimpleNamespace(get_frame_time=lambda: 1060000))
        after = canvas.convert_geographic_to_screen(MapPoint(47.6, -122.4))
        self.assertGreater(after[0], before[0])
        self.assertGreater(after[1], before[1])
        canvas._set_pan_inertia(False)
        self.assertIsNone(canvas._inertia)
        self.assertEqual(canvas._motion_tick, 0)


class TestDisplayPreferences(unittest.TestCase):
    def test_legacy_defaults_live_changes_and_persistence(self):
        from gweatherrouting.gtk.settings.settingsmanager import SettingsManager

        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"chartPalette": "dark"}))
            with patch("gweatherrouting.core.storage.DATA_DIR", directory):
                settings = SettingsManager()
                self.assertTrue(settings.smoothZoom)
                self.assertTrue(settings.panInertia)
                self.assertEqual(settings.gribBarbDensity, "standard")
                canvas = MotionMap()
                canvas.bind_display_settings(settings)
                settings.smoothZoom = False
                settings.panInertia = False
                self.assertFalse(canvas._smooth_zoom)
                self.assertFalse(canvas._pan_inertia_enabled)
                reloaded = SettingsManager()
                self.assertFalse(reloaded.smoothZoom)
                self.assertFalse(reloaded.panInertia)
                self.assertEqual(reloaded.chartPalette, "dark")
                canvas._destroy_motion()
                settings.smoothZoom = True
                self.assertFalse(canvas._smooth_zoom)

    def test_settings_window_display_switches_are_connected(self):
        from gweatherrouting.gtk.settings.settingsmanager import SettingsManager
        from gweatherrouting.gtk.settings.settingswindow import SettingsWindow
        from gweatherrouting.core.marine.coverage import CoveragePlanner

        with TemporaryDirectory() as directory:
            with patch("gweatherrouting.core.storage.DATA_DIR", directory):
                settings = SettingsManager()
                parent = SimpleNamespace(chart_manager=SimpleNamespace(charts=[]))
                core = SimpleNamespace(
                    connectionManager=SimpleNamespace(connections=[]),
                    marine_manager=SimpleNamespace(coverage_planner=CoveragePlanner()),
                )
                window = SettingsWindow(parent, settings, core)
                stack = window.builder.get_object("stack1")
                display = window.builder.get_object("display-tab")
                self.assertEqual(stack.child_get_property(display, "title"), "Display")
                for widget, key in (
                    ("smooth-zoom", "smoothZoom"),
                    ("pan-inertia", "panInertia"),
                ):
                    switch = window.builder.get_object(widget)
                    self.assertTrue(switch.get_active())
                    switch.set_active(False)
                    self.assertFalse(settings[key])
                density = window.builder.get_object("grib-barb-density")
                self.assertEqual(density.get_active_id(), "standard")
                density.set_active_id("all")
                self.assertEqual(settings.gribBarbDensity, "all")
                self.assertEqual(SettingsManager().gribBarbDensity, "all")
                window.window.destroy()
