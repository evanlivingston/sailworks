"""Small, headless regression checks for the native acceptance helpers."""

import importlib.util
import math
from pathlib import Path
from unittest.mock import Mock

import numpy as np
from PySide6.QtCore import QPoint, QPointF, Qt
from types import SimpleNamespace

from gweatherrouting.qt.gribviewport import _viewport_widget_class


def tool():
    path = Path(__file__).resolve().parents[1] / "tools/check_enc_viewport.py"
    spec = importlib.util.spec_from_file_location("enc_gate_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nested_geometry_identity():
    assert tool()._rounded([[(1.12345678, 2.0)]]) == (((1.123457, 2.0),),)


def test_fidelity_identical_and_blank_negative():
    module = tool()
    reference = np.zeros((10, 10, 4), dtype=np.uint8)
    reference[3:7, 3:7] = (10, 20, 30, 255)
    empty = np.zeros_like(reference)
    metrics = module._fidelity(reference, reference, empty)
    assert metrics["mae_255"] == 0
    assert metrics["coverage"] == 1
    assert metrics["alpha_coverage"] == 1
    assert metrics["nonblank"]
    assert not module._fidelity(empty, reference, empty)["nonblank"]


def test_weather_miss_keeps_enc_scene_visible():
    def item():
        state = SimpleNamespace(visible=None)
        state.setVisible = lambda visible: setattr(state, "visible", visible)
        return state

    widget = SimpleNamespace(
        _enc_chart_layer=True, _weather=item(), _barbs=item(), _fill=item()
    )
    method = _viewport_widget_class()._set_weather_visible
    method(widget, False)
    assert widget._weather.visible
    assert not widget._barbs.visible
    assert not widget._fill.visible
    method(widget, True)
    assert widget._barbs.visible and widget._fill.visible


def test_default_viewport_still_hides_weather_scene_on_miss():
    scene = SimpleNamespace(visible=True)
    scene.setVisible = lambda visible: setattr(scene, "visible", visible)
    widget = SimpleNamespace(_enc_chart_layer=False, _weather=scene)
    _viewport_widget_class()._set_weather_visible(widget, False)
    assert not scene.visible


def test_desktop_native_pinch_zooms_the_viewport_at_the_gesture_anchor():
    method = _viewport_widget_class()._native_gesture_event
    widget = SimpleNamespace(_native_gesture_active=False, zoom_at=Mock())

    class Gesture:
        def gestureType(self):
            return Qt.NativeGestureType.ZoomNativeGesture

        def value(self):
            return 0.3

        def position(self):
            return QPointF(123, 234)

        def accept(self):
            self.accepted = True

    event = Gesture()
    assert method(widget, event)
    assert widget._native_gesture_active
    widget.zoom_at.assert_called_once_with(math.log2(1.3), 123, 234)
    assert event.accepted


def test_desktop_native_gesture_end_clears_viewport_gesture_state():
    method = _viewport_widget_class()._native_gesture_event
    widget = SimpleNamespace(_native_gesture_active=True)

    class Gesture:
        def gestureType(self):
            return Qt.NativeGestureType.EndNativeGesture

        def accept(self):
            self.accepted = True

    event = Gesture()
    assert method(widget, event)
    assert not widget._native_gesture_active
    assert event.accepted


def test_desktop_precise_scroll_pans_the_viewport_camera():
    method = _viewport_widget_class()._handle_desktop_wheel
    widget = SimpleNamespace(
        _native_gesture_active=False,
        _wheel_zoom_tick=Mock(),
        _wheel_zoom_settle=Mock(),
        _wheel_scroll_settle=Mock(),
        _wheel_zoom_target=9.0,
        _wheel_zoom_anchor=(10, 10, 1, 2),
        _wheel_trackpad_panning=False,
        pan_pixels=Mock(),
    )

    class Scroll:
        def phase(self):
            return Qt.ScrollPhase.ScrollUpdate

        def pixelDelta(self):
            return QPoint(12, -7)

        def modifiers(self):
            return Qt.KeyboardModifier.NoModifier

        def accept(self):
            self.accepted = True

    event = Scroll()
    method(widget, event)

    widget.pan_pixels.assert_called_once_with(12, -7)
    assert widget._wheel_trackpad_panning
    assert widget._wheel_zoom_target is None
    assert widget._wheel_zoom_anchor is None
    assert event.accepted


def test_desktop_mouse_wheel_zoom_keeps_the_pointer_anchor():
    from datetime import datetime

    from gweatherrouting.qt.gribviewport import ViewportCamera

    method = _viewport_widget_class()._handle_desktop_wheel
    camera = ViewportCamera(48.45, -124.29, 9.0, 710, 743, datetime(2026, 10, 22))
    zoom_tick = Mock()
    zoom_tick.isActive.return_value = False
    widget = SimpleNamespace(
        _camera=camera,
        _native_gesture_active=False,
        _wheel_zoom_tick=zoom_tick,
        _wheel_zoom_settle=Mock(),
        _wheel_scroll_settle=Mock(),
        _wheel_zoom_target=None,
        _wheel_zoom_anchor=None,
        _wheel_trackpad_panning=False,
        _smooth_zoom=False,
        set_camera=Mock(),
    )
    widget._set_zoom_around = lambda zoom, x, y, anchor=None: (
        _viewport_widget_class()._set_zoom_around(widget, zoom, x, y, anchor)
    )
    widget._finish_wheel_zoom = lambda: _viewport_widget_class()._finish_wheel_zoom(
        widget
    )

    class Wheel:
        def phase(self):
            return Qt.ScrollPhase.NoScrollPhase

        def pixelDelta(self):
            return QPoint()

        def angleDelta(self):
            return QPoint(0, 120)

        def modifiers(self):
            return Qt.KeyboardModifier.NoModifier

        def position(self):
            return QPointF(123, 234)

        def accept(self):
            self.accepted = True

    event = Wheel()
    view = camera.view()
    anchor = (
        view._y_to_lat(view._lat_to_y(camera.center_lat) + 234 - camera.height / 2),
        view._x_to_lon(view._lon_to_x(camera.center_lon) + 123 - camera.width / 2),
    )
    method(widget, event)

    updated = widget.set_camera.call_args.args[0]
    updated_view = updated.view()
    assert updated.zoom == camera.zoom + 0.25
    assert (
        abs(
            updated_view._lat_to_y(anchor[0])
            - updated_view._lat_to_y(updated.center_lat)
            - 234
            + camera.height / 2
        )
        < 1e-8
    )
    assert (
        abs(
            updated_view._lon_to_x(anchor[1])
            - updated_view._lon_to_x(updated.center_lon)
            - 123
            + camera.width / 2
        )
        < 1e-8
    )
    assert event.accepted


def test_world_chart_scene_stays_visible_when_weather_is_unavailable():
    scene = SimpleNamespace(visible=True)
    scene.setVisible = lambda visible: setattr(scene, "visible", visible)
    barbs = SimpleNamespace(visible=True)
    barbs.setVisible = lambda visible: setattr(barbs, "visible", visible)
    widget = SimpleNamespace(
        _chart_layer=True,
        _weather=scene,
        _barbs=barbs,
        _fill=None,
    )

    _viewport_widget_class()._set_weather_visible(widget, False)

    assert scene.visible
    assert not barbs.visible


def test_initial_weather_miss_does_not_block_enc_request():
    requests = []
    widget = SimpleNamespace(
        _chart_model=SimpleNamespace(request=requests.append),
        _camera="camera",
        _weather_ready_at=None,
        _enc_chart_layer=True,
        _model=SimpleNamespace(state="unavailable", busy=lambda: False),
        _chart_camera_changed=0,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )
    method = _viewport_widget_class()._request_chart
    method(widget)
    assert requests == ["camera"]
    widget._model.busy = lambda: True
    method(widget)
    assert requests == ["camera"]


def test_world_basemap_request_does_not_wait_for_weather():
    requests = []
    widget = SimpleNamespace(
        _chart_model=SimpleNamespace(request=requests.append),
        _camera="camera",
        _weather_ready_at=None,
        _chart_without_weather=True,
        _enc_chart_layer=False,
        _model=SimpleNamespace(state="loading", busy=lambda: False),
        _chart_camera_changed=0,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )

    _viewport_widget_class()._request_chart(widget)

    assert requests == ["camera"]


def test_text_size_observer_forwards_the_actual_draw(monkeypatch):
    from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

    drawn = []

    def original(drawer, gpsmap, context, feature):
        return context.show_text("12.3")

    monkeypatch.setattr(S57ChartDrawer, "_render_soundings", original)
    context = SimpleNamespace(
        text_extents=lambda text: (0, 0, 20, 11, 20, 0),
        user_to_device_distance=lambda width, height: (width * 2, height * 2),
        show_text=drawn.append,
    )
    observer = tool()._TextSizes()
    restore = observer.install()
    try:
        observer.stage = "zoom"
        S57ChartDrawer.__new__(S57ChartDrawer)._render_soundings(None, context, None)
        assert drawn == ["12.3"]
        assert observer.sizes["zoom"]["12.3"] == (40, 22)
    finally:
        restore()
    assert S57ChartDrawer._render_soundings is original


def test_close_withdraws_retained_image_and_worker_releases_base_handles(tmp_path):
    from gweatherrouting.qt.gribviewportchart import ViewportChartLayer

    layer = ViewportChartLayer((), tmp_path)
    layer._published = (1, object(), {})
    layer._payload_bytes = 4096
    released = []
    layer._composer_instance = SimpleNamespace(
        _renderer=SimpleNamespace(_release_resources=lambda: released.append(True))
    )
    layer.close()
    assert layer._published is None
    assert layer.observations()["payload_bytes"] == 0
    layer._release()
    assert released == [True]
    assert layer._composer_instance is None
